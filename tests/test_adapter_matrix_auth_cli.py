"""Tests for medre.adapters.matrix.cli and the adapter matrix auth login CLI surface.

Covers: parser structure, required flag enforcement, help output, and
integration flow with mocked login/whoami/update functions.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.cli.main import _build_parser


# ---------------------------------------------------------------------------
# Parser structure tests
# ---------------------------------------------------------------------------

class TestAdapterMatrixAuthParserStructure:
    """Parser-level tests for ``medre adapter matrix auth login``."""

    def test_adapter_matrix_auth_login_accepted(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--config", "/tmp/c.toml",
            "--adapter-id", "bot",
            "--homeserver", "https://m.org",
            "--user", "@b:m.org",
        ])
        assert args.command == "adapter"
        assert args.adapter_command == "matrix"
        assert args.adapter_matrix_command == "auth"
        assert args.adapter_matrix_auth_command == "login"
        assert args.config == "/tmp/c.toml"
        assert args.adapter_id == "bot"
        assert args.homeserver == "https://m.org"
        assert args.user == "@b:m.org"
        assert args.password_stdin is False

    def test_password_stdin_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--config", "/tmp/c.toml",
            "--adapter-id", "bot",
            "--homeserver", "https://m.org",
            "--user", "@b:m.org",
            "--password-stdin",
        ])
        assert args.password_stdin is True

    def test_adapter_requires_subcommand(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["adapter"])

    def test_adapter_matrix_requires_subcommand(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["adapter", "matrix"])

    def test_login_requires_config(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--adapter-id", "bot",
                "--homeserver", "https://m.org",
                "--user", "@b:m.org",
            ])

    def test_login_requires_adapter(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--config", "/tmp/c.toml",
                "--homeserver", "https://m.org",
                "--user", "@b:m.org",
            ])

    def test_login_requires_homeserver(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--config", "/tmp/c.toml",
                "--adapter-id", "bot",
                "--user", "@b:m.org",
            ])

    def test_login_requires_user(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--config", "/tmp/c.toml",
                "--adapter-id", "bot",
                "--homeserver", "https://m.org",
            ])

    def test_adapter_abbrev_rejected(self) -> None:
        """--adapter must not be accepted as abbreviation for --adapter-id."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--config", "/tmp/c.toml",
                "--adapter", "bot",
                "--homeserver", "https://m.org",
                "--user", "@bot:m.org",
            ])


# ---------------------------------------------------------------------------
# Help and SDK import safety
# ---------------------------------------------------------------------------

class TestHelpNoSdkImport:
    """Ensure --help does not import the Matrix SDK."""

    def test_medre_help_no_nio(self) -> None:
        """Building parser and rendering help must not import nio."""
        parser = _build_parser()
        # Rendering help just exercises parser formatting
        parser.format_help()
        # Verify the parser module source has lazy imports and no nio
        import medre.cli.main as _main_module
        # Access the module via sys.modules to get the actual module object
        import importlib
        main_mod = importlib.import_module("medre.cli.main")
        source = Path(main_mod.__file__).read_text()
        # The adapter dispatch branch imports contrib lazily
        assert "from .contrib import" in source
        # No direct nio import
        assert "import nio" not in source
        assert "from nio" not in source

    def test_adapter_matrix_auth_login_help(self) -> None:
        parser = _build_parser()
        # Should not raise
        out = io.StringIO()
        try:
            parser.parse_args(["adapter", "matrix", "auth", "login", "--help"])
        except SystemExit as exc:
            # --help exits with 0
            assert exc.code == 0


# ---------------------------------------------------------------------------
# Integration flow (monkeypatched)
# ---------------------------------------------------------------------------

class TestAdapterMatrixAuthLoginIntegration:
    """Integration tests for ``_adapter_matrix_auth_login`` with mocked internals."""

    def _make_args(self, **overrides: object) -> SimpleNamespace:
        defaults = {
            "config": "/tmp/test.toml",
            "adapter_id": "mybot",
            "homeserver": "https://matrix.org",
            "user": "@bot:matrix.org",
            "password_stdin": True,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_full_success_flow(self, tmp_path: Path) -> None:
        """Successful login, whoami, and TOML update."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        # Create a minimal TOML
        toml_path = tmp_path / "test.toml"
        toml_path.write_text(
            "[adapters.matrix.mybot]\n"
            'access_token = "old"\n',
            encoding="utf-8",
        )

        args = self._make_args(
            config=str(toml_path),
            password_stdin=True,
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            device_id="DEV_123",
            access_token="syt_secret_token",
        )

        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:matrix.org") as mock_whoami,
            patch("medre.adapters.matrix.auth.update_toml_credentials") as mock_update,
            patch("sys.stdin", io.StringIO("test_password\n")),
        ):
            await _adapter_matrix_auth_login(args)

            mock_login.assert_called_once_with(
                "https://matrix.org", "@bot:matrix.org", "test_password"
            )
            mock_whoami.assert_called_once_with("https://matrix.org", "syt_secret_token")
            mock_update.assert_called_once_with(
                toml_path, "matrix", "mybot",
                homeserver="https://matrix.org",
                user_id="@bot:matrix.org",
                access_token="syt_secret_token",
            )

    @pytest.mark.asyncio
    async def test_token_not_in_stdout(self, tmp_path: Path) -> None:
        """Token must never appear in stdout."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        toml_path = tmp_path / "test.toml"
        toml_path.write_text(
            "[adapters.matrix.bot]\naccess_token = \"old\"\n"
        )

        secret = "syt_SUPER_SECRET_TOKEN_9999"
        args = self._make_args(
            config=str(toml_path),
            user="@bot:m.org",
            homeserver="https://m.org",
            password_stdin=True,
        )

        login_result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@bot:m.org",
            device_id="DEV",
            access_token=secret,
        )

        stdout_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:m.org"),
            patch("medre.adapters.matrix.auth.update_toml_credentials"),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stdout", stdout_buf),
        ):
            await _adapter_matrix_auth_login(args)

        output = stdout_buf.getvalue()
        assert secret not in output

    @pytest.mark.asyncio
    async def test_login_error_exits_1(self) -> None:
        """Login failure should print to stderr and exit 1."""
        from medre.adapters.matrix.errors import MatrixConnectionError
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        stderr_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login",
                  side_effect=MatrixConnectionError("Login failed")),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "Login failed" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_whoami_error_exits_1(self) -> None:
        """Whoami failure should exit 1."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.errors import MatrixConnectionError
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        login_result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@b:m.org",
            device_id="D",
            access_token="tok",
        )

        stderr_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami",
                  side_effect=MatrixConnectionError("Token invalid")),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "Token invalid" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_missing_config_file_exits_1(self) -> None:
        """FileNotFoundError from update should exit 1."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(
            password_stdin=True,
            user="@b:m.org",
            homeserver="https://m.org",
        )

        login_result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@b:m.org",
            device_id="D",
            access_token="tok",
        )

        stderr_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@b:m.org"),
            patch("medre.adapters.matrix.auth.update_toml_credentials",
                  side_effect=FileNotFoundError("Config file not found: /nope")),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "Config file not found" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_getpass_used_when_no_stdin_flag(self) -> None:
        """Without --password-stdin, getpass.getpass is called."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(
            password_stdin=False,
            user="@b:m.org",
            homeserver="https://m.org",
        )

        login_result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@b:m.org",
            device_id="D",
            access_token="tok",
        )

        with (
            patch("medre.adapters.matrix.cli.getpass.getpass", return_value="interactive_pw") as mock_gp,
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@b:m.org"),
            patch("medre.adapters.matrix.auth.update_toml_credentials"),
        ):
            await _adapter_matrix_auth_login(args)

            mock_gp.assert_called_once_with("Matrix password: ")
            mock_login.assert_called_once_with(
                "https://m.org", "@b:m.org", "interactive_pw"
            )

    @pytest.mark.asyncio
    async def test_empty_password_exits_1(self) -> None:
        """Empty password from stdin should exit 1."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        stderr_buf = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO("\n")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "password is required" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_output_contains_expected_fields(self, tmp_path: Path) -> None:
        """Successful output should contain homeserver, user_id, device_id, config, adapter."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        toml_path = tmp_path / "c.toml"
        toml_path.write_text("[adapters.matrix.bot]\naccess_token = \"old\"\n")

        args = self._make_args(
            config=str(toml_path),
            adapter_id="bot",
            user="@alice:matrix.example.com",
            homeserver="https://matrix.example.com",
            password_stdin=True,
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.example.com",
            user_id="@alice:matrix.example.com",
            device_id="DEVICE_XYZ",
            access_token="tok",
        )

        stdout_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami",
                  return_value="@alice:matrix.example.com"),
            patch("medre.adapters.matrix.auth.update_toml_credentials"),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stdout", stdout_buf),
        ):
            await _adapter_matrix_auth_login(args)

        output = stdout_buf.getvalue()
        assert "https://matrix.example.com" in output
        assert "@alice:matrix.example.com" in output
        assert "DEVICE_XYZ" in output
        assert str(toml_path) in output
        assert "bot" in output
        assert "room" in output.lower() or "Reminder" in output

    @pytest.mark.asyncio
    async def test_whoami_mismatch_exits_1(self) -> None:
        """whoami user_id different from requested user_id should exit 1."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        login_result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@alice:example.com",
            device_id="DEV",
            access_token="tok",
        )

        stderr_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bob:example.com"),
            patch("medre.adapters.matrix.auth.update_toml_credentials"),
            patch("sys.stdin", io.StringIO("pw\n")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "does not match" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_homeserver_user_id_written_to_toml(self, tmp_path: Path) -> None:
        """update_toml_credentials writes homeserver, user_id, access_token to TOML."""
        from medre.adapters.matrix.auth import MatrixLoginResult, update_toml_credentials as real_fn
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        toml_path = tmp_path / "test.toml"
        toml_path.write_text(
            "[adapters.matrix.mybot]\n"
            'homeserver = ""\n'
            'user_id = ""\n'
            'access_token = ""\n',
            encoding="utf-8",
        )

        args = self._make_args(
            config=str(toml_path),
            user="@bot:matrix.example.com",
            homeserver="https://matrix.example.com",
            password_stdin=True,
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.example.com",
            user_id="@bot:matrix.example.com",
            device_id="DEV_999",
            access_token="syt_written_token",
        )

        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:matrix.example.com"),
            patch("sys.stdin", io.StringIO("pw\n")),
        ):
            # Use the real update_toml_credentials (not mocked)
            await _adapter_matrix_auth_login(args)

        import tomllib
        data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        section = data["adapters"]["matrix"]["mybot"]
        assert section["homeserver"] == "https://matrix.example.com"
        assert section["user_id"] == "@bot:matrix.example.com"
        assert section["access_token"] == "syt_written_token"

        # Verify chmod 0600
        import stat
        mode = toml_path.stat().st_mode
        assert not (mode & stat.S_IRGRP)
        assert not (mode & stat.S_IROTH)
