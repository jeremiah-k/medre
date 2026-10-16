# medre-test-tier: compatibility
"""Installed adapter CLI contributions participate in core command dispatch."""

from unittest.mock import patch

from tests.helpers.cli import _run_cli


def test_main_adapter_command_dispatches() -> None:
    """`medre adapter ...` dispatches through contributed command routing."""
    with patch("medre.cli.contrib.dispatch_contribution") as dispatch:
        _run_cli("adapter", "matrix", "auth", "status")

    dispatch.assert_called_once()
    args = dispatch.call_args.args[0]
    assert args.command == "adapter"
    assert args.adapter_command == "matrix"
    assert args.adapter_matrix_command == "auth"
    assert args.adapter_matrix_auth_command == "status"
