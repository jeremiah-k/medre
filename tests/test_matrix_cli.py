"""Tests for Matrix CLI commands.

Covers:
  - Line 96-97 — user_id None guard in _adapter_matrix_auth_login
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


class TestCliAuthLoginUserIdGuard:
    """Cover lines 96-97 — RuntimeError when user_id is None after tristate dispatch."""

    async def test_user_id_none_after_partial_dispatch_raises_runtime_error(
        self,
    ) -> None:
        """The guard at line 96 is a defensive check for an impossible-but-possible
        state after tristate dispatch. We reach it by mocking sys.exit to be a no-op
        so execution falls through the partial-credentials branch to the guard."""
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        # Construct args where provided > 0 (has password) but user_id is None
        args = MagicMock()
        args.homeserver = None
        args.user = None  # user_id stays None
        args.password = "secret"  # triggers provided > 0
        args.password_stdin = False

        # The partial branch calls sys.exit(1) at line 91.
        # Replace sys.exit with a no-op so execution continues to line 96.
        original_exit = sys.exit
        sys.exit = lambda code=0: None  # type: ignore[assignment]

        try:
            with pytest.raises(RuntimeError, match="user_id is None"):
                await _adapter_matrix_auth_login(args)
        finally:
            sys.exit = original_exit
