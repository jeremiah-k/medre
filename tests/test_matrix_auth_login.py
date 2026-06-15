"""Tests for medre.adapters.matrix.auth — login, whoami, sidecar JSON.

All network calls are mocked via ``urllib.request.urlopen`` patches.
No Matrix SDK (nio) is imported.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from medre.adapters.matrix.auth import (
    MatrixLoginResult,
    _normalize_homeserver,
    check_credentials_completeness,
    discover_well_known,
    extract_domain_from_mxid,
    matrix_login,
    matrix_whoami,
    save_credentials_json,
)
from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix_credentials import (
    get_credentials_path,
    load_credentials_json,
)

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
# _normalize_homeserver tests
# ---------------------------------------------------------------------------


class TestNormalizeHomeserver:
    """Tests for ``_normalize_homeserver``."""

    def test_bare_domain_gets_https_prefix(self) -> None:
        assert _normalize_homeserver("matrix.org") == "https://matrix.org"

    def test_bare_domain_with_port(self) -> None:
        assert _normalize_homeserver("matrix.org:8448") == "https://matrix.org:8448"

    def test_bare_domain_with_trailing_slash(self) -> None:
        assert _normalize_homeserver("matrix.org/") == "https://matrix.org/"

    def test_full_https_pass_through(self) -> None:
        assert _normalize_homeserver("https://matrix.org") == "https://matrix.org"

    def test_full_https_with_path_pass_through(self) -> None:
        assert (
            _normalize_homeserver("https://matrix.org:8448")
            == "https://matrix.org:8448"
        )

    def test_http_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            _normalize_homeserver("http://matrix.org")

    def test_ftp_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unsupported"):
            _normalize_homeserver("ftp://matrix.org")

    def test_whitespace_stripped(self) -> None:
        assert _normalize_homeserver("  matrix.org  ") == "https://matrix.org"


# ---------------------------------------------------------------------------
# matrix_login tests
# ---------------------------------------------------------------------------


class TestMatrixLogin:
    """Tests for ``matrix_login``."""

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "syt_secret123",
                "device_id": "DEVICE_ABC",
                "user_id": "@alice:matrix.org",
            }
        )

        result = matrix_login("https://matrix.org", "@alice:matrix.org", "hunter2")

        assert result == MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@alice:matrix.org",
            device_id="DEVICE_ABC",
            access_token="syt_secret123",
        )

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success_strips_trailing_slash(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "tok",
                "device_id": "DEV",
                "user_id": "@b:matrix.org",
            }
        )

        result = matrix_login("https://matrix.org/", "@b:matrix.org", "pw")
        assert result.homeserver == "https://matrix.org"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "tok",
                "device_id": "D",
                "user_id": "@u:m.org",
            }
        )

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

        mock_urlopen.side_effect = urllib.error.URLError(reason="Connection refused")

        with pytest.raises(MatrixConnectionError, match="network error"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_access_token(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "user_id": "@u:m.org",
                "device_id": "D",
            }
        )

        with pytest.raises(MatrixConnectionError, match="missing access_token"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_user_id(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "tok",
                "device_id": "D",
            }
        )

        with pytest.raises(MatrixConnectionError, match="missing user_id"):
            matrix_login("https://m.org", "@u:m.org", "pw")

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_device_id_defaults_empty(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "tok",
                "user_id": "@u:m.org",
            }
        )

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

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_bare_domain_normalized_to_https(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "access_token": "tok",
                "device_id": "D",
                "user_id": "@u:matrix.org",
            }
        )

        result = matrix_login("matrix.org", "@u:matrix.org", "pw")
        assert result.homeserver == "https://matrix.org"

        # Verify the URL passed to urlopen starts with the normalised form.
        call_args = mock_urlopen.call_args
        url_used = call_args[0][0].full_url
        assert url_used.startswith("https://matrix.org/")


# ---------------------------------------------------------------------------
# matrix_whoami tests
# ---------------------------------------------------------------------------


class TestMatrixWhoami:
    """Tests for ``matrix_whoami``."""

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "user_id": "@alice:matrix.org",
            }
        )

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
        error_body = f"Token {token} is invalid"
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
# No nio import verification
# ---------------------------------------------------------------------------


class TestNoSdkImport:
    """Verify that importing auth does not pull in nio."""

    def test_importing_auth_does_not_import_nio(self) -> None:
        """The auth module must not import nio or any Matrix SDK."""
        # Check that 'nio' is not in sys.modules after importing auth
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
# extract_domain_from_mxid tests
# ---------------------------------------------------------------------------


class TestExtractDomainFromMxid:
    """Tests for ``extract_domain_from_mxid``."""

    def test_full_user_id(self) -> None:
        assert extract_domain_from_mxid("@bot:sk.community") == "sk.community"

    def test_full_user_id_no_at(self) -> None:
        assert extract_domain_from_mxid("bot:sk.community") is None

    def test_localpart_only(self) -> None:
        assert extract_domain_from_mxid("bot") is None

    def test_empty(self) -> None:
        assert extract_domain_from_mxid("") is None

    def test_at_only(self) -> None:
        assert extract_domain_from_mxid("@") is None

    def test_with_port(self) -> None:
        assert extract_domain_from_mxid("@bot:matrix.org:8448") == "matrix.org:8448"

    def test_multiple_colons(self) -> None:
        assert extract_domain_from_mxid("@user:domain:8448") == "domain:8448"


# ---------------------------------------------------------------------------
# discover_well_known tests
# ---------------------------------------------------------------------------


class TestDiscoverWellKnown:
    """Tests for ``discover_well_known``."""

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_success_returns_base_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse(
            {
                "m.homeserver": {"base_url": "https://matrix.sk.community"},
            }
        )

        result = discover_well_known("sk.community")
        assert result == "https://matrix.sk.community"

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_404_returns_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = _FakeHTTPError(404)

        result = discover_well_known("sk.community")
        assert result is None

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_timeout_returns_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("timeout")

        result = discover_well_known("sk.community")
        assert result is None

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_malformed_json_returns_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({}, status=200)
        # Override read to return invalid JSON bytes
        mock_urlopen.return_value.read = lambda: b"not json at all"

        result = discover_well_known("sk.community")
        assert result is None

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_missing_homeserver_key_returns_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _FakeResponse({"other": "data"})

        result = discover_well_known("sk.community")
        assert result is None

    @patch("medre.adapters.matrix.auth.urllib.request.urlopen")
    def test_network_error_returns_none(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Name or service not known")

        result = discover_well_known("sk.community")
        assert result is None


# ---------------------------------------------------------------------------
# get_credentials_path tests
# ---------------------------------------------------------------------------


class TestGetCredentialsPath:
    """Tests for ``get_credentials_path``."""

    def test_default_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

        result = get_credentials_path()

        assert (
            result == Path.home() / ".config" / "medre" / "credentials" / "matrix.json"
        )

    def test_xdg_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom")

        result = get_credentials_path()

        assert result == Path("/custom") / "medre" / "credentials" / "matrix.json"


# ---------------------------------------------------------------------------
# save_credentials_json tests
# ---------------------------------------------------------------------------


class TestSaveCredentialsJson:
    """Tests for ``save_credentials_json``."""

    def test_writes_correct_json(self, tmp_path: Path) -> None:
        dest = tmp_path / "matrix.json"
        result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@alice:matrix.org",
            device_id="DEV123",
            access_token="syt_secret",
        )
        saved = save_credentials_json(result, path=dest)

        assert saved == dest
        assert dest.exists()
        data = json.loads(dest.read_text(encoding="utf-8"))
        assert data["homeserver"] == "https://matrix.org"
        assert data["user_id"] == "@alice:matrix.org"
        assert data["device_id"] == "DEV123"
        assert data["access_token"] == "syt_secret"

    def test_creates_directory(self, tmp_path: Path) -> None:
        dest = tmp_path / "subdir" / "credentials" / "matrix.json"
        result = MatrixLoginResult(
            homeserver="https://matrix.org",
            user_id="@alice:matrix.org",
            device_id="D",
            access_token="tok",
        )
        save_credentials_json(result, path=dest)

        assert dest.parent.exists()
        assert dest.exists()


# ---------------------------------------------------------------------------
# load_credentials_json tests
# ---------------------------------------------------------------------------


class TestLoadCredentialsJson:
    """Tests for ``load_credentials_json``."""

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.json"
        assert load_credentials_json(path=missing) is None

    def test_valid_file_returns_dict(self, tmp_path: Path) -> None:
        dest = tmp_path / "exists.json"
        dest.write_text(
            json.dumps(
                {
                    "homeserver": "https://matrix.org",
                    "access_token": "tok",
                    "user_id": "@u:m.org",
                    "device_id": "D",
                }
            ),
            encoding="utf-8",
        )

        result = load_credentials_json(path=dest)
        assert result is not None
        assert result["homeserver"] == "https://matrix.org"
        assert result["access_token"] == "tok"

    def test_malformed_json_returns_none(self, tmp_path: Path) -> None:
        dest = tmp_path / "bad.json"
        dest.write_text("not json", encoding="utf-8")

        assert load_credentials_json(path=dest) is None


# ---------------------------------------------------------------------------
# check_credentials_completeness tests
# ---------------------------------------------------------------------------


class TestCheckCredentialsCompleteness:
    """Tests for ``check_credentials_completeness``."""

    def test_all_present(self) -> None:
        result = check_credentials_completeness(
            {
                "homeserver": "x",
                "access_token": "x",
                "user_id": "x",
                "device_id": "x",
            }
        )
        assert result == []

    def test_missing_homeserver(self) -> None:
        result = check_credentials_completeness(
            {
                "access_token": "x",
                "user_id": "x",
            }
        )
        assert result == ["homeserver"]

    def test_missing_access_token(self) -> None:
        result = check_credentials_completeness(
            {
                "homeserver": "x",
                "user_id": "x",
            }
        )
        assert result == ["access_token"]

    def test_missing_user_id(self) -> None:
        result = check_credentials_completeness(
            {
                "homeserver": "x",
                "access_token": "x",
            }
        )
        assert result == ["user_id"]

    def test_all_missing(self) -> None:
        result = check_credentials_completeness({})
        assert result == ["homeserver", "access_token", "user_id"]

    def test_empty_string_values(self) -> None:
        result = check_credentials_completeness(
            {
                "homeserver": "",
                "access_token": "x",
                "user_id": "x",
            }
        )
        assert result == ["homeserver"]

    def test_device_id_optional(self) -> None:
        result = check_credentials_completeness(
            {
                "homeserver": "x",
                "access_token": "x",
                "user_id": "x",
            }
        )
        assert result == []
