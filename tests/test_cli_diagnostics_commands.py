"""Tests for 'medre diagnostics', diagnostics --refresh-health, and secret redaction."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.cli import (
    CONFIG_NO_ADAPTERS,
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
    p.write_text("""\
[runtime]
name = "test-routes"

[logging]
level = "INFO"

[storage]
backend = "memory"

[adapters.matrix.main]
enabled = true
adapter_kind = "fake"
homeserver = "https://matrix.test"
user_id = "@bot:test"
access_token = "tok"
room_allowlist = ["!room:test"]
encryption_mode = "plaintext"

[adapters.meshtastic.radio]
enabled = true
adapter_kind = "fake"
connection_type = "serial"
serial_port = "/dev/ttyACM0"
meshnet_name = "TestMesh"

[routes.matrix_to_radio]
source_adapters = ["main"]
dest_adapters = ["radio"]
directionality = "source_to_dest"
enabled = true
""")
    return p


@pytest.fixture()
def config_no_adapters(tmp_path: Path) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(CONFIG_NO_ADAPTERS)
    return p


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics:
    """Tests for 'medre diagnostics' command."""

    def test_diagnostics_produces_json(self, config_with_routes: Path) -> None:
        """Diagnostics with valid config produces parseable JSON output."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_diagnostics_json_has_adapters_key(self, config_with_routes: Path) -> None:
        """Diagnostics JSON contains adapter information."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        assert len(parsed) > 0

    def test_diagnostics_missing_config(self, tmp_path: Path) -> None:
        """Missing config file exits nonzero with clear error."""
        _, stderr = _run_cli_both(
            "diagnostics", "--config", str(tmp_path / "missing.toml")
        )
        assert "Config error:" in stderr
        assert "Traceback" not in stderr


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    """Verify that CLI output does not contain access tokens or passwords."""

    def test_config_check_no_secrets(self, config_with_routes: Path) -> None:
        """Config check output must not contain access tokens."""
        output = _run_cli("config", "check", "--config", str(config_with_routes))
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
# diagnostics --refresh-health
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
        self,
        fake_single_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health with fake adapters produces parseable JSON."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics",
            "--refresh-health",
            "--config",
            str(fake_single_config),
        )
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_refresh_health_has_live_health(
        self,
        fake_single_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health populates health.live_health (not null)."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics",
            "--refresh-health",
            "--config",
            str(fake_single_config),
        )
        parsed = json.loads(output)
        health = parsed["health"]
        assert health["live_health"] is not None
        assert health["live_refresh"] is True
        assert health["scope"] == "live"

    def test_refresh_health_has_poll_count(
        self,
        fake_single_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health snapshot has poll_count=1 from single refresh."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics",
            "--refresh-health",
            "--config",
            str(fake_single_config),
        )
        parsed = json.loads(output)
        live_health = parsed["health"]["live_health"]
        assert live_health["poll_count"] == 1

    def test_refresh_health_has_adapter_entries(
        self,
        fake_single_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--refresh-health populates per-adapter live health entries."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics",
            "--refresh-health",
            "--config",
            str(fake_single_config),
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
        self,
        config_with_routes: Path,
    ) -> None:
        """--refresh-health startup failure exits EXIT_STARTUP (4)."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from medre.cli import EXIT_STARTUP
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
                    "diagnostics",
                    "--refresh-health",
                    "--config",
                    str(config_with_routes),
                )
        assert exc_info.value.code == EXIT_STARTUP

    def test_refresh_health_startup_failure_no_traceback(
        self,
        config_with_routes: Path,
    ) -> None:
        """Startup failure produces clean error, no traceback."""
        from unittest.mock import AsyncMock, MagicMock, patch

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
                "diagnostics",
                "--refresh-health",
                "--config",
                str(config_with_routes),
            )
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr
        assert "Runtime startup failed:" in stderr

    def test_refresh_health_build_error_exits_3(
        self,
        config_with_routes: Path,
    ) -> None:
        """--refresh-health build failure exits EXIT_BUILD (3)."""
        from unittest.mock import patch

        from medre.cli import EXIT_BUILD

        with patch(
            "medre.runtime.builder.RuntimeBuilder.build",
            side_effect=RuntimeError("simulated build failure"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_cli(
                    "diagnostics",
                    "--refresh-health",
                    "--config",
                    str(config_with_routes),
                )
        assert exc_info.value.code == EXIT_BUILD

    def test_refresh_health_config_error_exits_2(self, tmp_path: Path) -> None:
        """--refresh-health with missing config exits EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "diagnostics",
                "--refresh-health",
                "--config",
                str(tmp_path / "missing.toml"),
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_refresh_health_no_adapters_exits_2(
        self,
        config_no_adapters: Path,
    ) -> None:
        """--refresh-health with no enabled adapters exits EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli(
                "diagnostics",
                "--refresh-health",
                "--config",
                str(config_no_adapters),
            )
        assert exc_info.value.code == EXIT_CONFIG

    def test_refresh_health_startup_health_remains_frozen(
        self,
        fake_single_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """startup.startup_health is frozen/separate from live health."""
        for var in ("MEDRE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("MEDRE_HOME", str(tmp_path))

        output = _run_cli(
            "diagnostics",
            "--refresh-health",
            "--config",
            str(fake_single_config),
        )
        parsed = json.loads(output)
        assert "startup" in parsed
        startup = parsed["startup"]
        assert startup["scope"] == "startup"
        assert startup["live_refresh"] is False
        health = parsed["health"]
        assert health["scope"] == "live"
        assert health["live_refresh"] is True
        assert health["live_health"] is not None

    def test_plain_diagnostics_unchanged_by_refresh_flag(
        self,
        config_with_routes: Path,
    ) -> None:
        """Plain 'medre diagnostics' (no --refresh-health) still works."""
        output = _run_cli("diagnostics", "--config", str(config_with_routes))
        parsed = json.loads(output)
        assert parsed["health"]["live_health"] is None
        assert parsed["health"]["live_refresh"] is False
        assert parsed["health"]["scope"] == "startup"
