"""Optional live Matrix auth tests.

Skipped by default.  Requires the following environment variables to be set:

  - MATRIX_HOMESERVER
  - MATRIX_USER_ID
  - MATRIX_PASSWORD

These tests do NOT pollute the standard test run.
"""

from __future__ import annotations

import os
import tomllib

import pytest

pytestmark = pytest.mark.live

require_matrix_auth = pytest.mark.skipif(
    not all(
        os.environ.get(v)
        for v in ("MATRIX_HOMESERVER", "MATRIX_USER_ID", "MATRIX_PASSWORD")
    ),
    reason="Set MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_PASSWORD for live auth test",
)


@require_matrix_auth
class TestMatrixAuthLive:
    """Live tests against a real Matrix homeserver using password login."""

    def test_login_and_whoami(self) -> None:
        from medre.adapters.matrix.auth import matrix_login, matrix_whoami

        result = matrix_login(
            homeserver=os.environ["MATRIX_HOMESERVER"],
            user_id=os.environ["MATRIX_USER_ID"],
            password=os.environ["MATRIX_PASSWORD"],
        )

        assert result.access_token, "access_token must be non-empty"
        assert result.device_id, "device_id must be non-empty"
        assert result.user_id == os.environ["MATRIX_USER_ID"]

        who = matrix_whoami(result.homeserver, result.access_token)
        assert who == os.environ["MATRIX_USER_ID"]

    def test_login_writes_to_config(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from medre.adapters.matrix.auth import matrix_login, update_toml_credentials

        result = matrix_login(
            homeserver=os.environ["MATRIX_HOMESERVER"],
            user_id=os.environ["MATRIX_USER_ID"],
            password=os.environ["MATRIX_PASSWORD"],
        )

        config_path = tmp_path / "test.toml"
        config_path.write_text(
            "[adapters.matrix.mybot]\n"
            'homeserver = ""\n'
            'user_id = ""\n'
            'access_token = ""\n',
            encoding="utf-8",
        )

        update_toml_credentials(
            config_path,
            "matrix",
            "mybot",
            homeserver=result.homeserver,
            user_id=result.user_id,
            access_token=result.access_token,
        )

        with config_path.open("rb") as f:
            data = tomllib.load(f)

        section = data["adapters"]["matrix"]["mybot"]
        assert section["access_token"] == result.access_token
        assert section["homeserver"] == result.homeserver
        assert section["user_id"] == result.user_id
