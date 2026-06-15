"""Optional live Matrix auth tests.

Skipped by default.  Requires the following environment variables to be set:

  - MATRIX_HOMESERVER
  - MATRIX_USER_ID
  - MATRIX_PASSWORD

These tests do NOT pollute the standard test run.
"""

from __future__ import annotations

import os

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
