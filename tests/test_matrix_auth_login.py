"""Tests for medre.adapters.matrix.auth — login, whoami, TOML update.

All network calls are mocked via ``urllib.request.urlopen`` patches.
No Matrix SDK (nio) is imported.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.matrix.auth import (
    MatrixLoginResult,
    matrix_login,
    matrix_whoami,
    update_toml_access_token,
    update_toml_credentials,
    _update_toml_field,
)
from medre.adapters.matrix.errors import MatrixConnectionError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal stand-in for ``http.client.HTTPResponse``."""

    def __init__(self, body: dict, status: int = 200) -> None:
        self._body = json.dumps(body).encode("utf-8")
        self.status = status
        self.code = status
        self._read = False

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class _FakeHTTPError(Exception):
    """Mimics ``urllib.error.HTTPError`` for testing."""

    def __init__(self, code: int, body: str = "") -> None:
        self.code = code
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body


# ---------------------------------------------------------------------------
# matrix_login tests
# ---------------------------------------------------------------------------

class TestMatrixLogin:
    """Tests for ``matrix_login``."""

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "access_token": "syt_secret123",
            "device_id": "DEVICE_ABC",
            "user_id": "@alice:matrix.org",
        })

        result = matrix_login("https://matrix.org", "@alice:matrix.org", "hunter2")

        assert result == MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@alice:matrix.org",
            device_id="DEVICE_ABC",
            access_token="syt_secret123",
        )

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success_strips_trailing_slash(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "access_token": "tok",
            "device_id": "DEV",
            "user_id": "@b:matrix.org",
        })

        result = matrix_login("https://matrix.org/", "@b:matrix.org", "pw")
        assert result.homeserver == "https://matrix.org"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "access_token": "tok",
            "device_id": "D",
            "user_id": "@u:m.org",
        })

        matrix_login("https://m.org", "@u:m.org", "pw")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        sent = json.loads(req.data)
        assert sent == {
            "type": "m.login.password",
            "user": "@u:m.org",
            "password": "pw",
        }
        assert req.get_header("Content-type") == "application/json"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_http_error(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://m.org/_matrix/client/v3/login",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b'{"errcode":"M_FORBIDDEN","error":"Invalid password"}'),
        )

        with pytest.raises(MatrixConnectionError, match="Login failed.*HTTP 403"):
            matrix_login("https://m.org", "@u:m.org", "wrong")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError(
            reason="Connection refused"
        )

        with pytest.raises(MatrixConnectionError, match="network error"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_access_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "user_id": "@u:m.org",
            "device_id": "D",
        })

        with pytest.raises(MatrixConnectionError, match="missing access_token"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_user_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "access_token": "tok",
            "device_id": "D",
        })

        with pytest.raises(MatrixConnectionError, match="missing user_id"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_device_id_defaults_empty(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "access_token": "tok",
            "user_id": "@u:m.org",
        })

        result = matrix_login("https://m.org", "@u:m.org", "pw")
        assert result.device_id == ""

    def test_result_repr_no_token(self) -> None:
        result = MatrixLoginResult(
            homeserver="https://m.org",
            user_id="@u:m.org",
            device_id="D",
            access_token="super_secret_token",
        )
        r = repr(result)
        assert "super_secret_token" not in r
        assert "***" in r


# ---------------------------------------------------------------------------
# matrix_whoami tests
# ---------------------------------------------------------------------------

class TestMatrixWhoami:
    """Tests for ``matrix_whoami``."""

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({
            "user_id": "@alice:matrix.org",
        })

        user_id = matrix_whoami("https://matrix.org", "syt_token")
        assert user_id == "@alice:matrix.org"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_sends_bearer_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({"user_id": "@u:m.org"})

        matrix_whoami("https://m.org", "my_token")

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my_token"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_http_error_redacts_token(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        token = "syt_super_secret_12345"
        error_body = f'Token {token} is invalid'
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://m.org/_matrix/client/v3/account/whoami",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(error_body.encode()),
        )

        with pytest.raises(MatrixConnectionError) as exc_info:
            matrix_whoami("https://m.org", token)

        error_msg = str(exc_info.value)
        assert token not in error_msg
        assert "***" in error_msg

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_network_error(self, mock_urlopen: MagicMock) -> None:
        import urllib.error

        mock_urlopen.side_effect = urllib.error.URLError(reason="Timeout")

        with pytest.raises(MatrixConnectionError, match="network error"):
            matrix_whoami("https://m.org", "tok")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_user_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({})

        with pytest.raises(MatrixConnectionError, match="missing user_id"):
            matrix_whoami("https://m.org", "tok")


# ---------------------------------------------------------------------------
# update_toml_access_token tests
# ---------------------------------------------------------------------------

class TestUpdateTomlAccessToken:
    """Tests for ``update_toml_access_token``."""

    def _write_toml(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_updates_token_in_section(self, tmp_path: Path) -> None:
        toml_content = (
            '# medre config\n'
            '[general]\n'
            'log_level = "info"\n'
            '\n'
            '[adapters.matrix.mybot]\n'
            'homeserver = "https://matrix.org"\n'
            'user_id = "@bot:matrix.org"\n'
            'access_token = "old_token"\n'
            'room_allowlist = ["!room:matrix.org"]\n'
            '\n'
            '[adapters.matrix.otherbot]\n'
            'access_token = "other_token"\n'
        )
        p = self._write_toml(tmp_path, toml_content)

        update_toml_access_token(p, "matrix", "mybot", "new_secret_token")

        updated = p.read_text(encoding="utf-8")
        assert 'access_token = "new_secret_token"' in updated
        assert 'old_token' not in updated
        assert '"other_token"' in updated  # other section unchanged
        # Comments preserved
        assert "# medre config" in updated

    def test_chmod_0600(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.bot]\n'
            'access_token = "old"\n'
        ))

        update_toml_access_token(p, "matrix", "bot", "new")

        mode = p.stat().st_mode
        assert mode & stat.S_IRUSR  # owner read
        assert mode & stat.S_IWUSR  # owner write
        assert not (mode & stat.S_IRGRP)  # no group read
        assert not (mode & stat.S_IWGRP)  # no group write
        assert not (mode & stat.S_IROTH)  # no other read
        assert not (mode & stat.S_IWOTH)  # no other write

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent.toml"
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            update_toml_access_token(p, "matrix", "bot", "tok")

    def test_missing_section_raises(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[general]\n'
            'log_level = "info"\n'
        ))

        with pytest.raises(ValueError, match="not found"):
            update_toml_access_token(p, "matrix", "bot", "tok")

    def test_missing_access_token_key_raises(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.bot]\n'
            'homeserver = "https://matrix.org"\n'
        ))

        with pytest.raises(ValueError, match="access_token key not found"):
            update_toml_access_token(p, "matrix", "bot", "tok")

    def test_preserves_comments_and_formatting(self, tmp_path: Path) -> None:
        toml_content = (
            '# Top comment\n'
            '[general]  # inline comment\n'
            'log_level = "info"\n'
            '\n'
            '# Matrix adapter\n'
            '[adapters.matrix.mybot]\n'
            'homeserver = "https://matrix.org"  # homeserver\n'
            'access_token = "old"\n'
        )
        p = self._write_toml(tmp_path, toml_content)

        update_toml_access_token(p, "matrix", "mybot", "new")

        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "# Top comment"
        assert "inline comment" in lines[1]
        assert "# Matrix adapter" in lines[4]
        assert "homeserver" in lines[6]

    def test_single_quoted_token(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            "[adapters.matrix.bot]\n"
            "access_token = 'old_token'\n"
        ))

        update_toml_access_token(p, "matrix", "bot", "new_token")

        updated = p.read_text(encoding="utf-8")
        assert 'access_token = "new_token"' in updated

    def test_different_adapter_names(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.alpha]\n'
            'access_token = "alpha_old"\n'
            '\n'
            '[adapters.matrix.beta]\n'
            'access_token = "beta_old"\n'
        ))

        update_toml_access_token(p, "matrix", "beta", "beta_new")

        updated = p.read_text(encoding="utf-8")
        assert '"alpha_old"' in updated  # alpha untouched
        assert 'access_token = "beta_new"' in updated


class TestTomlEscaping:
    """Verify update_toml_access_token escapes special characters so the
    written TOML parses back to the original token value."""

    def _write_toml(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def _roundtrip(self, tmp_path: Path, token: str) -> str:
        """Write *token*, parse the file with tomllib, return stored value."""
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.bot]\n'
            'access_token = "old"\n'
        ))
        update_toml_access_token(p, "matrix", "bot", token)
        data = tomllib.loads(p.read_text(encoding="utf-8"))
        return data["adapters"]["matrix"]["bot"]["access_token"]

    def test_plain_token_roundtrips(self, tmp_path: Path) -> None:
        assert self._roundtrip(tmp_path, "syt_plain_token") == "syt_plain_token"

    def test_double_quote_escaped(self, tmp_path: Path) -> None:
        token = 'token"with"quotes'
        assert self._roundtrip(tmp_path, token) == token

    def test_backslash_escaped(self, tmp_path: Path) -> None:
        token = "back\\slash"
        assert self._roundtrip(tmp_path, token) == token

    def test_newline_escaped(self, tmp_path: Path) -> None:
        token = "line1\nline2"
        assert self._roundtrip(tmp_path, token) == token

    def test_carriage_return_escaped(self, tmp_path: Path) -> None:
        token = "before\rafter"
        assert self._roundtrip(tmp_path, token) == token

    def test_tab_escaped(self, tmp_path: Path) -> None:
        token = "col1\tcol2"
        assert self._roundtrip(tmp_path, token) == token

    def test_control_char_escaped(self, tmp_path: Path) -> None:
        token = "before\x01after"
        assert self._roundtrip(tmp_path, token) == token

    def test_combined_special_chars(self, tmp_path: Path) -> None:
        token = 'a\\b"c\td\n'
        assert self._roundtrip(tmp_path, token) == token

    def test_written_file_is_valid_toml(self, tmp_path: Path) -> None:
        """Token with mixed special characters produces parseable TOML."""
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.bot]\n'
            'access_token = "old"\n'
        ))
        update_toml_access_token(p, "matrix", "bot", 'x"y\\z\t\n')
        raw = p.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
        assert data["adapters"]["matrix"]["bot"]["access_token"] == 'x"y\\z\t\n'


# ---------------------------------------------------------------------------
# No nio import verification
# ---------------------------------------------------------------------------

class TestNoSdkImport:
    """Verify that importing auth does not pull in nio."""

    def test_importing_auth_does_not_import_nio(self) -> None:
        """The auth module must not import nio or any Matrix SDK."""
        # Check that 'nio' is not in sys.modules after importing auth
        nio_present = "nio" in sys.modules
        # Import fresh
        import importlib
        import medre.adapters.matrix.auth as auth_mod
        importlib.reload(auth_mod)

        # nio should still not be in sys.modules (unless some other test loaded it)
        # But at minimum, the auth module itself should not have imported it
        source = Path(auth_mod.__file__).read_text()
        assert "import nio" not in source
        assert "from nio" not in source


# ---------------------------------------------------------------------------
# update_toml_credentials tests
# ---------------------------------------------------------------------------

class TestUpdateTomlCredentials:
    """Tests for ``update_toml_credentials``."""

    def _write_toml(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "config.toml"
        p.write_text(content, encoding="utf-8")
        return p

    def test_writes_homeserver_user_id_access_token(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.mybot]\n'
            'homeserver = ""\n'
            'user_id = ""\n'
            'access_token = ""\n'
        ))

        update_toml_credentials(
            p, "matrix", "mybot",
            homeserver="https://matrix.example.com",
            user_id="@alice:example.com",
            access_token="syt_secret123",
        )

        data = tomllib.loads(p.read_text(encoding="utf-8"))
        section = data["adapters"]["matrix"]["mybot"]
        assert section["homeserver"] == "https://matrix.example.com"
        assert section["user_id"] == "@alice:example.com"
        assert section["access_token"] == "syt_secret123"

    def test_preserves_comments(self, tmp_path: Path) -> None:
        toml_content = (
            '# medre config\n'
            '[general]\n'
            'log_level = "info"\n'
            '\n'
            '# Matrix adapter section\n'
            '[adapters.matrix.mybot]\n'
            '# Homeserver URL\n'
            'homeserver = ""\n'
            '# User ID\n'
            'user_id = ""\n'
            '# Access token\n'
            'access_token = ""\n'
        )
        p = self._write_toml(tmp_path, toml_content)

        update_toml_credentials(
            p, "matrix", "mybot",
            homeserver="https://m.org",
            user_id="@b:m.org",
            access_token="tok",
        )

        updated = p.read_text(encoding="utf-8")
        assert "# medre config" in updated
        assert "# Matrix adapter section" in updated
        assert "# Homeserver URL" in updated
        assert "# User ID" in updated
        assert "# Access token" in updated

    def test_raises_valueerror_section_not_found(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[general]\n'
            'log_level = "info"\n'
        ))

        with pytest.raises(ValueError, match="not found"):
            update_toml_credentials(
                p, "matrix", "mymatrix",
                homeserver="https://m.org",
                user_id="@a:m.org",
                access_token="tok",
            )

    def test_raises_valueerror_key_not_found(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.mymatrix]\n'
            'homeserver = "https://m.org"\n'
        ))

        with pytest.raises(ValueError, match="key not found"):
            update_toml_credentials(
                p, "matrix", "mymatrix",
                homeserver="https://m.org",
                user_id="@a:m.org",
                access_token="tok",
            )

    def test_chmod_0600(self, tmp_path: Path) -> None:
        p = self._write_toml(tmp_path, (
            '[adapters.matrix.bot]\n'
            'homeserver = ""\n'
            'user_id = ""\n'
            'access_token = ""\n'
        ))

        update_toml_credentials(
            p, "matrix", "bot",
            homeserver="https://m.org",
            user_id="@b:m.org",
            access_token="tok",
        )

        mode = p.stat().st_mode
        assert mode & stat.S_IRUSR  # owner read
        assert mode & stat.S_IWUSR  # owner write
        assert not (mode & stat.S_IRGRP)  # no group read
        assert not (mode & stat.S_IWGRP)  # no group write
        assert not (mode & stat.S_IROTH)  # no other read
        assert not (mode & stat.S_IWOTH)  # no other write


# ---------------------------------------------------------------------------
# _update_toml_field tests
# ---------------------------------------------------------------------------

class TestUpdateTomlField:
    """Tests for ``_update_toml_field`` helper."""

    def test_replaces_correct_line(self) -> None:
        lines = [
            "[adapters.matrix.mybot]\n",
            'access_token = "old"\n',
        ]
        new_lines, found = _update_toml_field(
            lines, Path("/dummy"), "matrix", "mybot",
            "access_token", "new_secret",
        )
        assert found is True
        assert 'access_token = "new_secret"\n' in new_lines

    def test_does_not_touch_other_sections(self) -> None:
        lines = [
            "[adapters.matrix.alpha]\n",
            'access_token = "alpha_old"\n',
            "\n",
            "[adapters.matrix.beta]\n",
            'access_token = "beta_old"\n',
        ]
        new_lines, found = _update_toml_field(
            lines, Path("/dummy"), "matrix", "beta",
            "access_token", "beta_new",
        )
        assert found is True
        assert 'access_token = "alpha_old"\n' in new_lines
        assert 'access_token = "beta_new"\n' in new_lines

    def test_handles_double_quoted_and_single_quoted(self) -> None:
        # Double-quoted value
        lines_dq = [
            "[adapters.matrix.bot]\n",
            'access_token = "old_dq"\n',
        ]
        new_lines_dq, found_dq = _update_toml_field(
            lines_dq, Path("/dummy"), "matrix", "bot",
            "access_token", "new_val",
        )
        assert found_dq is True
        assert 'access_token = "new_val"\n' in new_lines_dq

        # Single-quoted value
        lines_sq = [
            "[adapters.matrix.bot]\n",
            "access_token = 'old_sq'\n",
        ]
        new_lines_sq, found_sq = _update_toml_field(
            lines_sq, Path("/dummy"), "matrix", "bot",
            "access_token", "new_val",
        )
        assert found_sq is True
        assert 'access_token = "new_val"\n' in new_lines_sq
