"""Tests for 'medre run' error handling, exit codes, and sample config + fake runtime."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.helpers.cli import (
    CONFIG_NO_ADAPTERS,
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
def config_no_adapters(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_NO_ADAPTERS)
    return p


# ---------------------------------------------------------------------------
# medre run — error handling
# ---------------------------------------------------------------------------


class TestRunErrors:
    """Tests that 'medre run' exits cleanly (no traceback) on config errors."""

    def test_run_missing_config_no_traceback(self, tmp_path: Path) -> None:
        """Missing config causes clear error, not raw traceback."""
        _, stderr = _run_cli_both("run", "--config", str(tmp_path / "missing.yaml"))
        assert "Traceback" not in stderr
        assert "Config error:" in stderr

    def test_run_no_adapters_exits_nonzero(self, config_no_adapters: Path) -> None:
        """Run with no enabled adapters exits nonzero with clear message."""
        _, stderr = _run_cli_both("run", "--config", str(config_no_adapters))
        assert "no adapters enabled" in stderr.lower() or "error" in stderr.lower()

    def test_run_no_adapters_clear_message(self, config_no_adapters: Path) -> None:
        """Error message mentions adapters, not a traceback."""
        _, stderr = _run_cli_both("run", "--config", str(config_no_adapters))
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
            _run_cli("run", "--config", str(tmp_path / "missing.yaml"))
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
            _, stderr = _run_cli_both("run", "--config", str(config_with_routes))
        assert "Traceback" not in stderr
        assert "Runtime build error:" in stderr

    def test_build_error_exit_code(self, config_with_routes: Path) -> None:
        """Runtime build failure exits with EXIT_BUILD (3)."""
        from unittest.mock import patch

        from medre.cli import EXIT_BUILD

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
            _run_cli("config", "check", "--config", str(tmp_path / "missing.yaml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_routes_validate_config_error_exit_code(self, tmp_path: Path) -> None:
        """Routes validate with missing config exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("routes", "validate", "--config", str(tmp_path / "missing.yaml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_diagnostics_config_error_exit_code(self, tmp_path: Path) -> None:
        """Diagnostics with missing config exits with EXIT_CONFIG (2)."""
        from medre.cli import EXIT_CONFIG

        with pytest.raises(SystemExit) as exc_info:
            _run_cli("diagnostics", "--config", str(tmp_path / "missing.yaml"))
        assert exc_info.value.code == EXIT_CONFIG

    def test_diagnostics_build_error_exit_code(self, config_with_routes: Path) -> None:
        """Diagnostics build failure exits with EXIT_BUILD (3)."""
        from unittest.mock import patch

        from medre.cli import EXIT_BUILD

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
        from unittest.mock import MagicMock, patch

        from medre.cli import EXIT_BUILD

        fake_app = MagicMock()
        fake_app.adapters = {}
        fake_app.build_failures = [
            MagicMock(
                transport="matrix",
                adapter_id="main",
                error=RuntimeError("missing SDK"),
            )
        ]

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
        from unittest.mock import MagicMock, patch

        fake_app = MagicMock()
        fake_app.adapters = {}
        fake_app.build_failures = [
            MagicMock(
                transport="matrix",
                adapter_id="main",
                error=RuntimeError("missing SDK"),
            )
        ]

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
        from unittest.mock import AsyncMock, MagicMock, patch

        from medre.cli import EXIT_STARTUP
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

    def test_startup_error_no_traceback(self, config_with_routes: Path) -> None:
        """Startup failure produces clean error message, no traceback."""
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
            stdout, stderr = _run_cli_both("run", "--config", str(config_with_routes))
        assert "Traceback" not in stdout
        assert "Traceback" not in stderr
        assert "Runtime startup failed:" in stdout

    def test_diagnostics_all_build_failure_exits_build(
        self, config_with_routes: Path
    ) -> None:
        """Diagnostics with all adapters failing construction exits EXIT_BUILD (3)."""
        from unittest.mock import MagicMock, patch

        from medre.cli import EXIT_BUILD

        fake_app = MagicMock()
        fake_app.adapters = {}
        fake_app.build_failures = [
            MagicMock(
                transport="matrix",
                adapter_id="main",
                error=RuntimeError("missing SDK"),
            )
        ]

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
        _, stderr = _run_cli_both("diagnostics", "--config", str(config_no_adapters))
        assert "no adapters enabled" in stderr.lower()
        assert "failed to construct" not in stderr.lower()

    def test_diagnostics_partial_build_succeeds(self, config_with_routes: Path) -> None:
        """Diagnostics with some adapters built, some failed, exits EXIT_OK (0)."""
        from unittest.mock import MagicMock, patch

        fake_app = MagicMock()
        fake_app.adapters = {"matrix.main": MagicMock()}
        fake_app.build_failures = [
            MagicMock(
                transport="matrix",
                adapter_id="backup",
                error=RuntimeError("missing SDK"),
            )
        ]

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
# Sample config parse + fake runtime assembly
# ---------------------------------------------------------------------------


class TestSampleConfigAndFakeRuntime:
    """Sample config parses correctly and a fake runtime can be assembled."""

    CONFIG_FAKE_MULTI = """\
runtime:
  name: fake-runtime-test
logging:
  level: DEBUG
storage:
  backend: memory
adapters:
  matrix:
    fake_mx:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.test
      user_id: '@fake:fake.test'
      access_token: fake_token_for_test
      room_allowlist: ['!fake:fake.test']
      encryption_mode: plaintext
  meshtastic:
    fake_mesh:
      enabled: true
      adapter_kind: fake
      connection_type: serial
      serial_port: /dev/ttyFAKE
      origin_label: FakeMesh
"""

    @pytest.fixture()
    def fake_config(self, tmp_path: Path) -> Path:
        p = tmp_path / "fake_config.yaml"
        p.write_text(self.CONFIG_FAKE_MULTI)
        return p

    def test_sample_config_parses(self) -> None:
        """generate_sample_config() produces valid YAML."""
        import yaml

        from medre.config.sample import generate_sample_config

        sample = generate_sample_config()
        parsed = yaml.safe_load(sample)
        assert "runtime" in parsed
        assert "adapters" in parsed

    @pytest.mark.asyncio
    async def test_fake_runtime_assembles_from_config(
        self,
        fake_config: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A config with adapter_kind='fake' assembles into a working MedreApp."""
        from medre.config.loader import load_config
        from medre.runtime.app import RuntimeState
        from medre.runtime.builder import RuntimeBuilder

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
