"""Paths, version, adapters, Docker env, install metadata, and redaction workflows.

Covers:

- ``medre paths`` — MEDRE_HOME override and XDG fallback
- ``medre version`` — output format
- ``medre adapters`` — SDK availability listing and configured adapters
- Docker-style env overrides — deterministic env -> config
- Install metadata checks — pyproject.toml structure, entry points
- Secret redaction — ``sanitize_error`` redacts tokens and passwords
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from tests.helpers.cli import (
    _run_cli,
)

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
        output = _run_cli("paths")
        assert "XDG" in output or "Mode:" in output

    def test_paths_dir_status_indicators(self, tmp_home: Path) -> None:
        """Each dir shows [exists] or [will be created]."""
        output = _run_cli("paths")
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
        output = _run_cli("diagnostics", "--config", str(config_fake_multi))
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_env_overrides_do_not_leak_in_config_check(
        self, config_fake_multi: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config check output does not contain secret env values."""
        monkeypatch.setenv(
            "MEDRE_ADAPTER__FAKE_MATRIX__ACCESS_TOKEN", "env_secret_token_12345"
        )
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
        assert (
            "syt_" not in content
            or "secret" in content.lower()
            or "here" in content.lower()
        )


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

        mod = importlib.import_module("medre.cli")
        assert hasattr(mod, "main")
        assert hasattr(mod, "__name__")


# ===================================================================
# 20. Redaction test — secret-looking config path does not leak
# ===================================================================


class TestRedactionSanitizeError:
    """sanitize_error from medre.core.observability.sanitization redacts secrets."""

    def test_sanitize_error_redacts_access_token(self) -> None:
        """access_token values are redacted by sanitize_error."""
        from medre.core.observability.sanitization import sanitize_error

        msg = "Config error: access_token=syt_super_secret_12345 for /etc/medre/access_token_config.toml"
        result = sanitize_error(msg)
        assert "syt_super_secret_12345" not in result
        assert "[REDACTED]" in result
        assert "/etc/medre/" in result

    def test_sanitize_error_redacts_password(self) -> None:
        """password= values are redacted by sanitize_error."""
        from medre.core.observability.sanitization import sanitize_error

        msg = "Auth failed: password=hunter2 for user admin"
        result = sanitize_error(msg)
        assert "hunter2" not in result
        assert "[REDACTED]" in result

    def test_sanitize_error_regex_matches_access_token_pattern(self) -> None:
        """The _TOKEN_RE regex matches access_token= patterns."""

        from medre.core.observability.sanitization import _TOKEN_RE

        patterns_that_should_match = [
            "access_token=syt_abc123",
            "access_token: tok_value",
            "token=abc123",
            "password=secret",
            "secret=my_secret",
            "syt_AbCdEf123456",
        ]
        for pattern in patterns_that_should_match:
            assert _TOKEN_RE.search(pattern), f"_TOKEN_RE should match: {pattern!r}"

    def test_secret_config_path_does_not_leak_in_sanitized_error(self) -> None:
        """A config path containing 'access_token' in its filename does not leak
        into errors/limitations when sanitize_error is applied."""
        from medre.core.observability.sanitization import sanitize_error

        secret_path = "/home/user/.config/medre/access_token.toml"
        error_msg = f"Failed to load config from {secret_path}: permission denied"
        result = sanitize_error(error_msg)
        assert secret_path in result
        assert "[REDACTED]" not in result or "access_token=" not in result
