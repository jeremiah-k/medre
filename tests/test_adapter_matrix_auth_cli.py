"""Tests for medre.adapters.matrix.cli and the adapter matrix auth CLI surface.

Covers: parser structure (tristate optional flags), status subcommand,
--password-stdin TTY guard, and integration flow with mocked
login/whoami/save_credentials_json (NOT update_toml_credentials).
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from medre.cli.main import _build_parser


def _pipe_stdin(text: str) -> io.StringIO:
    """Return a StringIO with a ``fileno`` stub (for ``os.isatty`` call)."""
    buf = io.StringIO(text)
    buf.fileno = lambda: 0  # type: ignore[attr-defined]
    return buf


# ---------------------------------------------------------------------------
# Parser structure tests
# ---------------------------------------------------------------------------

class TestAdapterMatrixAuthParserStructure:
    """Parser-level tests for ``medre adapter matrix auth login``."""

    def test_adapter_matrix_auth_login_accepted(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--homeserver", "https://m.org",
            "--user", "@b:m.org",
        ])
        assert args.command == "adapter"
        assert args.adapter_command == "matrix"
        assert args.adapter_matrix_command == "auth"
        assert args.adapter_matrix_auth_command == "login"
        assert args.homeserver == "https://m.org"
        assert args.user == "@b:m.org"
        assert args.password_stdin is False

    def test_password_stdin_flag(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
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

    def test_login_accepts_no_flags(self) -> None:
        """Homeserver and user are optional; minimal invocation parses."""
        parser = _build_parser()
        args = parser.parse_args(["adapter", "matrix", "auth", "login"])
        assert getattr(args, "homeserver", None) is None
        assert getattr(args, "user", None) is None

    def test_login_accepts_user_only_mxid(self) -> None:
        """Providing only --user (no --homeserver) is accepted."""
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--user", "@bot:server",
        ])
        assert args.homeserver is None
        assert args.user == "@bot:server"

    def test_password_flag_accepted(self) -> None:
        """--password flag is parsed and stored."""
        parser = _build_parser()
        args = parser.parse_args([
            "adapter", "matrix", "auth", "login",
            "--homeserver", "https://m.org",
            "--user", "@b:m.org",
            "--password", "somepass",
        ])
        assert args.password == "somepass"

    def test_status_subcommand_parses(self) -> None:
        """adapter matrix auth status is a valid subcommand."""
        parser = _build_parser()
        args = parser.parse_args(["adapter", "matrix", "auth", "status"])
        assert args.adapter_matrix_auth_command == "status"

    def test_unknown_flag_rejected(self) -> None:
        """Unknown flags are rejected by the parser."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "adapter", "matrix", "auth", "login",
                "--unknown", "value",
            ])


# ---------------------------------------------------------------------------
# Help and SDK import safety
# ---------------------------------------------------------------------------

class TestHelpNoSdkImport:
    """Ensure --help does not import the Matrix SDK."""

    def test_medre_help_no_nio(self) -> None:
        """Building parser and rendering help must not import nio."""
        parser = _build_parser()
        parser.format_help()
        import medre.cli.main as _main_module
        import importlib
        main_mod = importlib.import_module("medre.cli.main")
        source = Path(main_mod.__file__).read_text()
        assert "from .contrib import" in source
        assert "import nio" not in source
        assert "from nio" not in source

    def test_adapter_matrix_auth_login_help(self) -> None:
        parser = _build_parser()
        try:
            parser.parse_args(["adapter", "matrix", "auth", "login", "--help"])
        except SystemExit as exc:
            assert exc.code == 0


# ---------------------------------------------------------------------------
# --password-stdin TTY guard
# ---------------------------------------------------------------------------

class TestPasswordStdinTtyGuard:
    """Tests for the TTY guard when ``--password-stdin`` is used."""

    def _make_args(self, **overrides: object) -> SimpleNamespace:
        defaults = {
            "homeserver": "https://matrix.org",
            "user": "@bot:matrix.org",
            "password": None,
            "password_stdin": True,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_password_stdin_with_tty_exits_1(self) -> None:
        """--password-stdin on a TTY should exit 1 with 'piped input' message."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        stderr_buf = io.StringIO()
        with (
            patch("medre.adapters.matrix.cli.os.isatty", return_value=True),
            patch("sys.stdin", _pipe_stdin("")),
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        assert "piped input" in stderr_buf.getvalue()

    @pytest.mark.asyncio
    async def test_password_stdin_with_pipe_succeeds(self) -> None:
        """--password-stdin on a pipe (not TTY) should proceed normally."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(password_stdin=True)

        login_result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            device_id="DEV",
            access_token="tok",
        )

        pipe_stdin = io.StringIO("test_pw\n")
        pipe_stdin.fileno = lambda: 0  # type: ignore[attr-defined]

        with (
            patch("medre.adapters.matrix.cli.os.isatty", return_value=False),
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:matrix.org"),
            patch("medre.adapters.matrix.auth.update_toml_credentials"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=Path("/tmp/matrix.json")),
            patch("sys.stdin", pipe_stdin),
        ):
            await _adapter_matrix_auth_login(args)


# ---------------------------------------------------------------------------
# Login help epilog
# ---------------------------------------------------------------------------

class TestLoginHelpEpilog:
    """Tests for the login subcommand help epilog content."""

    def test_help_shows_example_command(self) -> None:
        """--help output should contain the example 'medre adapter matrix auth login' command."""
        parser = _build_parser()
        stdout_buf = io.StringIO()
        with (
            patch("sys.stdout", stdout_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            parser.parse_args(["adapter", "matrix", "auth", "login", "--help"])

        assert exc_info.value.code == 0
        output = stdout_buf.getvalue()
        assert "medre adapter matrix auth login" in output

    def test_help_mentions_homeserver_format(self) -> None:
        """--help output should mention bare domain / matrix.example.com."""
        parser = _build_parser()
        stdout_buf = io.StringIO()
        with (
            patch("sys.stdout", stdout_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            parser.parse_args(["adapter", "matrix", "auth", "login", "--help"])

        assert exc_info.value.code == 0
        output = stdout_buf.getvalue()
        assert "bare domain" in output or "matrix.example.com" in output


# ---------------------------------------------------------------------------
# Integration flow (monkeypatched) — REWRITTEN for tristate + JSON credentials
# ---------------------------------------------------------------------------

class TestAdapterMatrixAuthLoginIntegration:
    """Integration tests for ``_adapter_matrix_auth_login`` with mocked internals.

    Covers: interactive mode (prompts), non-interactive mode (all flags),
    partial flags error, homeserver derivation from MXID, well-known
    fallback, and credentials saved via save_credentials_json (NOT
    update_toml_credentials).
    """

    def _make_args(self, **overrides: object) -> SimpleNamespace:
        defaults: dict[str, object] = {
            "homeserver": None,
            "user": None,
            "password": None,
            "password_stdin": False,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    @pytest.mark.asyncio
    async def test_interactive_mode_prompts(self, tmp_path: Path) -> None:
        """Interactive mode (no flags): prompts via input()/getpass, full flow."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        args = self._make_args(config=None, adapter_id=None)

        login_result = MatrixLoginResult(
            homeserver="https://server",
            user_id="@bot:server",
            device_id="DEV_1",
            access_token="syt_interactive_tok",
        )

        stdout_buf = io.StringIO()
        with (
            patch("builtins.input", return_value="@bot:server") as mock_input,
            patch("medre.adapters.matrix.cli.getpass.getpass", return_value="pw") as mock_getpass,
            patch("medre.adapters.matrix.auth.extract_domain_from_mxid", return_value="server"),
            patch("medre.adapters.matrix.auth.discover_well_known", return_value=None),
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:server"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path) as mock_save,
            patch("sys.stdout", stdout_buf),
        ):
            await _adapter_matrix_auth_login(args)

        output = stdout_buf.getvalue()
        assert "https://server" in output
        assert "@bot:server" in output
        assert "DEV_1" in output
        assert str(cred_path) in output
        mock_input.assert_called_once()
        mock_getpass.assert_called_once()
        mock_save.assert_called_once_with(login_result)

    @pytest.mark.asyncio
    async def test_noninteractive_all_flags(self, tmp_path: Path) -> None:
        """Non-interactive mode: all flags set, no prompts."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        args = self._make_args(
            homeserver="https://matrix.org",
            user="@bot:matrix.org",
            password="pw123",
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            device_id="DEV_2",
            access_token="syt_noninteractive",
        )

        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:matrix.org"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path) as mock_save,
        ):
            await _adapter_matrix_auth_login(args)

            mock_login.assert_called_once_with(
                "https://matrix.org", "@bot:matrix.org", "pw123",
            )
            mock_save.assert_called_once_with(login_result)

    @pytest.mark.asyncio
    async def test_partial_flags_exits_1(self) -> None:
        """Partial flags (user but no password/pw_stdin) should exit 1."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        args = self._make_args(
            user="@bot:matrix.org",
        )

        stderr_buf = io.StringIO()
        with (
            patch("sys.stderr", stderr_buf),
            pytest.raises(SystemExit) as exc_info,
        ):
            await _adapter_matrix_auth_login(args)

        assert exc_info.value.code == 1
        msg = stderr_buf.getvalue()
        # Should contain guidance about providing all flags or none
        assert len(msg) > 0

    @pytest.mark.asyncio
    async def test_homeserver_derived_from_mxid(self, tmp_path: Path) -> None:
        """When user is MXID but no homeserver, derive from domain via well-known."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        args = self._make_args(
            user="@bot:sk.community",
            password="pw",
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.sk.community",
            user_id="@bot:sk.community",
            device_id="DEV_3",
            access_token="syt_derived",
        )

        with (
            patch("medre.adapters.matrix.auth.extract_domain_from_mxid", return_value="sk.community") as mock_extract,
            patch("medre.adapters.matrix.auth.discover_well_known", return_value="https://matrix.sk.community") as mock_wk,
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:sk.community"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path),
        ):
            await _adapter_matrix_auth_login(args)

            mock_extract.assert_called_once_with("@bot:sk.community")
            mock_wk.assert_called_once_with("sk.community")
            mock_login.assert_called_once_with(
                "https://matrix.sk.community", "@bot:sk.community", "pw",
            )

    @pytest.mark.asyncio
    async def test_wellknown_failure_uses_https_fallback(self, tmp_path: Path) -> None:
        """If well-known fails, homeserver falls back to https://{domain}."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        args = self._make_args(
            user="@bot:sk.community",
            password="pw",
        )

        login_result = MatrixLoginResult(
            homeserver="https://sk.community",
            user_id="@bot:sk.community",
            device_id="DEV_4",
            access_token="syt_fallback",
        )

        with (
            patch("medre.adapters.matrix.auth.extract_domain_from_mxid", return_value="sk.community"),
            patch("medre.adapters.matrix.auth.discover_well_known", return_value=None),
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result) as mock_login,
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:sk.community"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path),
        ):
            await _adapter_matrix_auth_login(args)

            mock_login.assert_called_once_with(
                "https://sk.community", "@bot:sk.community", "pw",
            )

    @pytest.mark.asyncio
    async def test_credentials_json_not_toml(self, tmp_path: Path) -> None:
        """Credentials saved via save_credentials_json, NOT update_toml_credentials."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        args = self._make_args(
            homeserver="https://matrix.org",
            user="@bot:matrix.org",
            password="pw",
        )

        login_result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@bot:matrix.org",
            device_id="DEV_5",
            access_token="syt_not_toml",
        )

        with (
            patch("medre.adapters.matrix.auth.matrix_login", return_value=login_result),
            patch("medre.adapters.matrix.auth.matrix_whoami", return_value="@bot:matrix.org"),
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path) as mock_save,
            patch("medre.adapters.matrix.auth.update_toml_credentials") as mock_toml,
        ):
            await _adapter_matrix_auth_login(args)

            mock_save.assert_called_once_with(login_result)
            mock_toml.assert_not_called()

    @pytest.mark.asyncio
    async def test_token_not_in_stdout(self, tmp_path: Path) -> None:
        """Token must never appear in stdout."""
        from medre.adapters.matrix.auth import MatrixLoginResult
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        cred_path = tmp_path / "matrix.json"
        secret = "syt_SUPER_SECRET_TOKEN_9999"
        args = self._make_args(
            homeserver="https://m.org",
            user="@bot:m.org",
            password="pw",
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
            patch("medre.adapters.matrix.auth.save_credentials_json", return_value=cred_path),
            patch("sys.stdout", stdout_buf),
        ):
            await _adapter_matrix_auth_login(args)

        output = stdout_buf.getvalue()
        assert secret not in output


# ---------------------------------------------------------------------------
# Status handler tests
# ---------------------------------------------------------------------------

class TestAdapterMatrixAuthStatus:
    """Tests for ``_adapter_matrix_auth_status`` handler."""

    @pytest.mark.asyncio
    async def test_status_no_file(self, tmp_path: Path) -> None:
        """Status when credentials file does not exist."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_status

        missing = tmp_path / "missing.json"
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            await _adapter_matrix_auth_status(credentials_path=missing)

        output = stdout_buf.getvalue()
        assert "No credentials file at:" in output or "No credentials" in output

    @pytest.mark.asyncio
    async def test_status_complete(self, tmp_path: Path) -> None:
        """Status with complete credentials shows checkmarks."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_status

        cred_file = tmp_path / "exists.json"
        cred_file.write_text(json.dumps({
            "homeserver": "https://matrix.org",
            "access_token": "syt_tok",
            "user_id": "@bot:matrix.org",
            "device_id": "DEV",
        }), encoding="utf-8")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            await _adapter_matrix_auth_status(credentials_path=cred_file)

        output = stdout_buf.getvalue()
        assert "Homeserver" in output
        assert "User ID" in output
        assert "✓" in output
        assert "complete" in output.lower()

    @pytest.mark.asyncio
    async def test_status_incomplete(self, tmp_path: Path) -> None:
        """Status with incomplete credentials shows missing fields."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_status

        cred_file = tmp_path / "incomplete.json"
        cred_file.write_text(json.dumps({
            "homeserver": "https://matrix.org",
            "user_id": "@bot:matrix.org",
            # access_token intentionally missing
        }), encoding="utf-8")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            await _adapter_matrix_auth_status(credentials_path=cred_file)

        output = stdout_buf.getvalue()
        assert "access_token" in output
        assert "Missing" in output or "missing" in output or "✗" in output

    @pytest.mark.asyncio
    async def test_status_malformed(self, tmp_path: Path) -> None:
        """Status with malformed JSON shows error."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_status

        cred_file = tmp_path / "bad.json"
        cred_file.write_text("not json", encoding="utf-8")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            await _adapter_matrix_auth_status(credentials_path=cred_file)

        output = stdout_buf.getvalue()
        assert "malformed" in output.lower() or "parse" in output.lower() or "invalid" in output.lower()
