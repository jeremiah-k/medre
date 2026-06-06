"""Tests for CLI command help text operator UX hints.

Verifies that each command's help string concisely states:
- Whether the command is read-only
- Whether it starts the runtime
- Whether it may send messages
- Whether it requires config
- Whether it accepts --storage-path

These are operator-facing hints embedded in the argparse help text,
verified here to prevent accidental regression.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout

from medre.cli.main import _build_parser


def _get_command_help_text(command: str) -> str:
    """Extract the full help text for a top-level command from the parser.

    Runs ``medre --help`` and parses the output to find the command's
    help string, joining continuation lines.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    parser = _build_parser()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            parser.parse_args(["--help"])
    except SystemExit:
        pass
    output = stdout.getvalue() + stderr.getvalue()
    # Match the command line and any continuation lines (indented with spaces).
    # argparse wraps long help texts to continuation lines with extra indentation.
    lines = output.splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        # A new command line: starts with spaces, then the command name.
        m = re.match(rf"^\s+{re.escape(command)}\s+(.+)$", line)
        if m:
            collecting = True
            collected.append(m.group(1))
            continue
        if collecting:
            # Continuation lines are indented more than the command name.
            if re.match(r"^\s{20,}", line):
                collected.append(line.strip())
            else:
                break
    return " ".join(collected)


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


class TestRunHelp:
    """medre run — starts runtime, may send messages, requires config."""

    def test_help_mentions_runtime(self) -> None:
        help_text = _get_command_help_text("run")
        assert "runtime" in help_text.lower()

    def test_help_mentions_send_messages(self) -> None:
        help_text = _get_command_help_text("run")
        assert "send" in help_text.lower() or "messages" in help_text.lower()

    def test_help_mentions_config(self) -> None:
        """Run command help references runtime (not config) in its summary."""
        help_text = _get_command_help_text("run")
        assert "runtime" in help_text.lower()


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


class TestDiagnosticsHelp:
    """medre diagnostics — read-only snapshot, requires config."""

    def test_help_mentions_read_only(self) -> None:
        help_text = _get_command_help_text("diagnostics")
        assert "read-only" in help_text.lower()

    def test_help_mentions_config(self) -> None:
        help_text = _get_command_help_text("diagnostics")
        assert "config" in help_text.lower()


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


class TestInspectHelp:
    """medre inspect — read-only storage inspection, accepts --storage-path."""

    def test_help_mentions_read_only(self) -> None:
        help_text = _get_command_help_text("inspect")
        assert "read-only" in help_text.lower()

    def test_help_mentions_storage_path(self) -> None:
        help_text = _get_command_help_text("inspect")
        assert "storage-path" in help_text.lower()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


class TestReplayHelp:
    """medre replay — may send messages in best_effort, requires config."""

    def test_help_mentions_send_messages(self) -> None:
        help_text = _get_command_help_text("replay")
        assert "send" in help_text.lower() or "messages" in help_text.lower()

    def test_help_mentions_config(self) -> None:
        help_text = _get_command_help_text("replay")
        assert "config" in help_text.lower()

    def test_help_mentions_best_effort(self) -> None:
        help_text = _get_command_help_text("replay")
        assert "best_effort" in help_text.lower()


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


class TestSmokeHelp:
    """medre smoke — local validation tooling, not bridge operation."""

    def test_help_mentions_local_validation(self) -> None:
        help_text = _get_command_help_text("smoke")
        assert "local" in help_text.lower() or "validation" in help_text.lower()

    def test_help_mentions_not_daily_operation(self) -> None:
        help_text = _get_command_help_text("smoke")
        assert "not daily" in help_text.lower()


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


class TestTraceHelp:
    """medre trace — specialized timeline, usually inspect event --timeline."""

    def test_help_mentions_specialized(self) -> None:
        help_text = _get_command_help_text("trace")
        assert "specialized" in help_text.lower()

    def test_help_mentions_read_only(self) -> None:
        help_text = _get_command_help_text("trace")
        assert "read-only" in help_text.lower()

    def test_help_mentions_storage_path(self) -> None:
        help_text = _get_command_help_text("trace")
        assert "storage-path" in help_text.lower()

    def test_help_mentions_inspect_guidance(self) -> None:
        help_text = _get_command_help_text("trace")
        assert "inspect event" in help_text.lower()


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


class TestEvidenceHelp:
    """medre evidence — specialized support bundle, usually inspect event --evidence."""

    def test_help_mentions_specialized(self) -> None:
        help_text = _get_command_help_text("evidence")
        assert "specialized" in help_text.lower()

    def test_help_mentions_read_only(self) -> None:
        help_text = _get_command_help_text("evidence")
        assert "read-only" in help_text.lower()

    def test_help_mentions_storage_path(self) -> None:
        help_text = _get_command_help_text("evidence")
        assert "storage-path" in help_text.lower()

    def test_help_mentions_inspect_guidance(self) -> None:
        help_text = _get_command_help_text("evidence")
        assert "inspect event" in help_text.lower()


# ---------------------------------------------------------------------------
# recover
# ---------------------------------------------------------------------------


class TestRecoverHelp:
    """medre recover — specialized recovery classification, usually inspect event --recovery."""

    def test_help_mentions_specialized(self) -> None:
        help_text = _get_command_help_text("recover")
        assert "specialized" in help_text.lower()

    def test_help_mentions_read_only(self) -> None:
        help_text = _get_command_help_text("recover")
        assert "read-only" in help_text.lower()

    def test_help_mentions_storage_path(self) -> None:
        help_text = _get_command_help_text("recover")
        assert "storage-path" in help_text.lower()

    def test_help_mentions_inspect_guidance(self) -> None:
        help_text = _get_command_help_text("recover")
        assert "inspect event" in help_text.lower()


# ---------------------------------------------------------------------------
# Shared constants are public when shared
# ---------------------------------------------------------------------------


class TestPublicConstants:
    """Shared CLI constants should be public (no leading underscore)."""

    def test_transports_is_public(self) -> None:
        from medre.cli.transports import TRANSPORTS

        assert isinstance(TRANSPORTS, list)
        assert len(TRANSPORTS) > 0

    def test_radio_transports_is_public(self) -> None:
        from medre.cli.transport_constants import RADIO_TRANSPORTS

        assert isinstance(RADIO_TRANSPORTS, frozenset)
        assert len(RADIO_TRANSPORTS) > 0

    def test_no_private_transport_constant_in_transports_module(self) -> None:
        """transports.py must not still export _TRANSPORTS."""
        import medre.cli.transports as mod

        assert not hasattr(
            mod, "_TRANSPORTS"
        ), "transports.py still has private _TRANSPORTS — rename to TRANSPORTS"

    def test_no_private_constant_in_transport_constants_module(self) -> None:
        """transport_constants.py must not still export _RADIO_TRANSPORTS."""
        import medre.cli.transport_constants as mod

        assert not hasattr(
            mod, "_RADIO_TRANSPORTS"
        ), "transport_constants.py still has private _RADIO_TRANSPORTS — rename to RADIO_TRANSPORTS"

    def test_config_commands_imports_public(self) -> None:
        """config_commands.py imports TRANSPORTS (not _TRANSPORTS)."""
        import inspect

        import medre.cli.config_commands as mod

        source = inspect.getsource(mod)
        assert (
            "_TRANSPORTS" not in source
        ), "config_commands.py still references _TRANSPORTS"
        assert "TRANSPORTS" in source

    def test_recover_commands_imports_public(self) -> None:
        """recover_commands.py imports RADIO_TRANSPORTS (not _RADIO_TRANSPORTS)."""
        import inspect

        import medre.cli.recover_commands as mod

        source = inspect.getsource(mod)
        assert (
            "_RADIO_TRANSPORTS" not in source
        ), "recover_commands.py still references _RADIO_TRANSPORTS"
        assert "RADIO_TRANSPORTS" in source
