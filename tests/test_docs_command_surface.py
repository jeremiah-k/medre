"""CLI command surface and adapter auth command tests.

Asserts that configuration.md documents all CLI commands and the adapter
auth command appears in the operator command surface.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
OPS_DIR = _ROOT / "docs" / "ops"
# TODO: operator-command-surface.md needs a home in the new docs tree
# _OPERATOR_COMMAND_SURFACE = (
#     _ROOT / "docs" / "architecture" / "operator-command-surface.md"
# )


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


# ===========================================================================
# 7. CLI command surface in configuration.md
# ===========================================================================


class TestConfigurationCliSurface:
    """configuration.md must document all top-level CLI commands."""

    REQUIRED_COMMANDS = [
        "smoke",
        "inspect",
        "trace",
        "evidence",
        "replay",
        "recover",
        "diagnostics",
        "routes",
        "adapters",
    ]

    @pytest.mark.parametrize("command", REQUIRED_COMMANDS)
    def test_cli_command_documented(self, command: str) -> None:
        text = _read(OPS_DIR / "configuration.md")
        # Check for the command as a subcommand header (e.g. "medre smoke")
        assert f"medre {command}" in text, (
            f"configuration.md CLI Commands section must document "
            f"'medre {command}'."
        )

    def test_storage_path_bypass_note_present(self) -> None:
        """configuration.md must note that --storage-path bypasses config
        for read-only commands."""
        text = _read(OPS_DIR / "configuration.md")
        assert "--storage-path" in text, (
            "configuration.md must mention --storage-path for read-only "
            "commands (inspect, trace event, evidence)."
        )

    def test_replay_rejects_storage_path_noted(self) -> None:
        """configuration.md must note that replay rejects --storage-path."""
        text = _read(OPS_DIR / "configuration.md")
        # Find the replay section and check it mentions the rejection
        assert "reject" in text.lower() or "requires --config" in text.lower(), (
            "configuration.md must note that replay requires --config "
            "and does not accept --storage-path."
        )

    def test_inspect_replay_documented(self) -> None:
        """configuration.md must document 'inspect replay' subcommand."""
        text = _read(OPS_DIR / "configuration.md")
        assert "inspect replay" in text, (
            "configuration.md must document 'medre inspect replay' "
            "as a read-only storage inspection subcommand."
        )

    def test_inspect_event_flags_documented(self) -> None:
        """configuration.md must document --timeline, --evidence, --recovery
        flags for inspect event."""
        text = _read(OPS_DIR / "configuration.md")
        for flag in ("--timeline", "--evidence", "--recovery"):
            assert flag in text, (
                f"configuration.md must document '{flag}' flag for "
                f"'medre inspect event'."
            )


# ===========================================================================
# Parser -> docs direction: every operator-facing top-level command in the
# parser must appear in configuration.md's CLI inventory.
# ===========================================================================


def _parser_top_level_commands() -> set[str]:
    """Extract the set of top-level command names from the CLI parser.

    Walks ``medre.cli.main._build_parser()`` and returns every name
    registered on the root subparsers. This reads the actual parser so the
    test reflects reality, not a hardcoded list.
    """
    from medre.cli.main import _build_parser

    parser = _build_parser()
    commands: set[str] = set()
    subparsers = getattr(parser, "_subparsers", None)
    if subparsers is None:
        return commands
    for action in subparsers._actions:
        choices = getattr(action, "choices", None)
        if choices:
            commands.update(choices.keys())
    return commands


class TestParserCommandsDocumented:
    """PARSER -> DOCS direction: every operator-facing top-level command
    produced by ``_build_parser()`` must be documented in the configuration.md
    CLI inventory. This is the reverse of the docs->parser check and catches
    commands added to the parser but never surfaced to operators."""

    def test_every_parser_command_documented_in_configuration_md(self) -> None:
        text = _read(OPS_DIR / "configuration.md")
        parser_commands = _parser_top_level_commands()
        assert parser_commands, "Parser unexpectedly exposed no top-level commands"

        # Every parser command is operator-facing (see operator-surface-audit);
        # there is no separate hidden/internal command set.
        undocumented = sorted(
            cmd for cmd in parser_commands if f"medre {cmd}" not in text
        )
        assert not undocumented, (
            "configuration.md CLI inventory is missing operator-facing "
            f"commands that exist in the parser: {undocumented}. "
            f"Parser commands: {sorted(parser_commands)}"
        )

    def test_full_command_set_matches_pc_expectation(self) -> None:
        """The parser command set must match the expected operator surface.

        Guards against silent additions or removals at the parser level.
        Adapter sub-namespaces (matrix auth login/status) are verified in
        a separate nested-coverage test in
        ``test_command_surface_and_status_consistency.py``.
        """
        parser_commands = _parser_top_level_commands()
        expected = {
            "run",
            "config",
            "paths",
            "version",
            "adapters",
            "diagnostics",
            "routes",
            "smoke",
            "inspect",
            "trace",
            "evidence",
            "replay",
            "recover",
            "storage",
            "adapter",
            "support",
        }
        missing = expected - parser_commands
        extra = parser_commands - expected
        assert not missing, (
            f"Parser is missing expected commands: {sorted(missing)}. "
            f"Available: {sorted(parser_commands)}"
        )
        assert not extra, (
            "Parser has commands not covered by the expected operator "
            f"surface: {sorted(extra)}. Update the expectation (and "
            "configuration.md) if these are operator-facing."
        )


# ===========================================================================
# 15. Alpha command surface freeze section present
# DISABLED: operator-command-surface.md was deleted during docs consolidation.
# TODO: operator-command-surface.md needs a home in the new docs tree.
# ===========================================================================


# class TestAlphaCommandSurfaceFreeze:
#     """operator-command-surface.md must include the alpha command surface
#     freeze section documenting the frozen command categories."""
#
#     def test_freeze_section_present(self) -> None:
#         """operator-command-surface.md must have an 'Alpha command surface
#         freeze' section."""
#         path = _OPERATOR_COMMAND_SURFACE
#         if not path.exists():
#             pytest.skip("operator-command-surface.md not found")
#         text = _read(path)
#         assert "Alpha command surface freeze" in text, (
#             "operator-command-surface.md must include an 'Alpha command "
#             "surface freeze' section."
#         )
#
#     def test_freeze_section_lists_categories(self) -> None:
#         """The freeze section must list Product, Validation, and Specialized
#         categories."""
#         path = _OPERATOR_COMMAND_SURFACE
#         if not path.exists():
#             pytest.skip("operator-command-surface.md not found")
#         text = _read(path)
#         freeze_start = text.find("Alpha command surface freeze")
#         if freeze_start < 0:
#             pytest.fail("Alpha command surface freeze section not found")
#         freeze_end = text.find("\n## ", freeze_start + 1)
#         if freeze_end < 0:
#             freeze_end = len(text)
#         section = text[freeze_start:freeze_end]
#         for category in (
#             "Product surface",
#             "Validation surface",
#             "Specialized surface",
#         ):
#             assert (
#                 category in section
#             ), f"Freeze section must list '{category}' category."
#
#     def test_freeze_section_states_inspect_preferred(self) -> None:
#         """The freeze section must state that inspect is the preferred
#         investigation path."""
#         path = _OPERATOR_COMMAND_SURFACE
#         if not path.exists():
#             pytest.skip("operator-command-surface.md not found")
#         text = _read(path)
#         freeze_start = text.find("Alpha command surface freeze")
#         if freeze_start < 0:
#             pytest.fail("Alpha command surface freeze section not found")
#         freeze_end = text.find("\n## ", freeze_start + 1)
#         if freeze_end < 0:
#             freeze_end = len(text)
#         section = text[freeze_start:freeze_end]
#         assert "preferred investigation path" in section.lower(), (
#             "Freeze section must state that inspect is the preferred "
#             "investigation path."
#         )


# ===========================================================================
# 28. Adapter auth command in operator command surface
# DISABLED: operator-command-surface.md was deleted during docs consolidation.
# TODO: operator-command-surface.md needs a home in the new docs tree.
# ===========================================================================


# class TestAdapterAuthCommandInOperatorSurface:
#     """operator-command-surface.md must list ``medre adapter matrix auth login``
#     in both the command inventory and the operational properties decision table."""
#
#     def test_auth_matrix_login_in_command_inventory(self) -> None:
#         """The command inventory must include ``adapter matrix auth login``."""
#         if not _OPERATOR_COMMAND_SURFACE.exists():
#             pytest.skip("operator-command-surface.md not found")
#         text = _read(_OPERATOR_COMMAND_SURFACE)
#         assert "adapter matrix auth login" in text.lower(), (
#             "operator-command-surface.md must include 'adapter matrix auth login' "
#             "in the command inventory."
#         )
#
#     def test_auth_in_decision_table(self) -> None:
#         """The operational properties decision table must include
#         ``adapter matrix auth login`` as a row."""
#         if not _OPERATOR_COMMAND_SURFACE.exists():
#             pytest.skip("operator-command-surface.md not found")
#         text = _read(_OPERATOR_COMMAND_SURFACE)
#         # Locate the decision table section.
#         dt_start = text.find("## Operational properties decision table")
#         if dt_start < 0:
#             pytest.fail(
#                 "operator-command-surface.md is missing the "
#                 "'Operational properties decision table' section."
#             )
#         dt_end = text.find("\n## ", dt_start + 1)
#         if dt_end < 0:
#             dt_end = len(text)
#         section = text[dt_start:dt_end]
#         assert "adapter matrix auth login" in section.lower(), (
#             "The decision table section must include an "
#             "'adapter matrix auth login' row."
#         )
