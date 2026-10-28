"""Navigate registered command help without executing command handlers."""

from __future__ import annotations

import argparse
import textwrap


class CommandHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wrap explanations without splitting option names or operational terms."""

    def _split_lines(self, text: str, width: int) -> list[str]:
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)


def help_request(arguments: list[str]) -> list[str]:
    """Accept help before a command path as well as the usual trailing flag."""
    if not arguments:
        return ["--help"]
    if arguments[0] == "help" or (arguments[0] == "--help" and len(arguments) > 1):
        return [*arguments[1:], "--help"]
    return arguments


def group_help(parser: argparse.ArgumentParser, arguments: list[str]) -> list[str]:
    """Show help for an exact command-group path, preserving real syntax errors.

    Only registered subcommand names are traversed. Options, values, unknown
    names and leaf commands retain argparse's ordinary validation. No guessed
    commands are executed.
    """
    current = parser
    for word in arguments:
        action = next(
            (
                item
                for item in current._actions
                if isinstance(item, argparse._SubParsersAction)
            ),
            None,
        )
        if action is None or word not in action.choices:
            return arguments
        current = action.choices[word]
    if any(isinstance(item, argparse._SubParsersAction) for item in current._actions):
        return [*arguments, "--help"]
    return arguments


def describe_defaults(parser: argparse.ArgumentParser) -> None:
    """Keep command usage compact and expose shared configuration discovery.

    Resolve command groups from the parser itself so provider-added commands
    receive the same presentation. Individual command-specific defaults, such
    as smoke's synthetic sample, remain owned by their original help strings.
    """
    if parser.formatter_class in {
        argparse.HelpFormatter,
        argparse.RawDescriptionHelpFormatter,
    }:
        parser.formatter_class = CommandHelpFormatter
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            action.metavar = "COMMAND"
            for child in action.choices.values():
                describe_defaults(child)
        elif action.dest == "config" and action.help == "Path to config file":
            action.help = (
                "Bridge YAML (default: saved selection or standard discovery; "
                "see 'medre config current')"
            )
