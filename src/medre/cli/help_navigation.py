"""Navigate registered command help without executing command handlers."""

from __future__ import annotations

import argparse
import textwrap


class CommandHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Wrap explanations without splitting option names or operational terms."""

    def _split_lines(self, text: str, width: int) -> list[str]:
        return textwrap.wrap(" ".join(text.split()), width, break_on_hyphens=False)


def help_request(arguments: list[str]) -> tuple[list[str], bool]:
    """Separate explicit help intent from arguments that command parsers consume.

    Appending a help flag cannot express that intent safely: a leaf parser's
    REMAINDER can consume the flag and dispatch a real command. Keep help as
    navigation until the registered parser is selected, before parsing values.
    """
    if not arguments:
        return [], True
    if arguments[0] in {"help", "--help"}:
        return arguments[1:], True
    return arguments, False


def show_command_help(parser: argparse.ArgumentParser, arguments: list[str]) -> None:
    """Print help for the deepest registered path without invoking a handler.

    Unknown commands inside groups remain syntax errors. At a leaf, remaining
    arguments are values or delegated arguments; they cannot change which
    MEDRE command's help is requested and are never executed or parsed.
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
        if action is None or word in {"-h", "--help"}:
            break
        if word not in action.choices:
            current.error(
                f"argument {action.dest}: invalid choice: {word!r} "
                f"(choose from {', '.join(action.choices)})"
            )
        current = action.choices[word]
    current.print_help()
    current.exit()


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
                "Bridge YAML (default: MEDRE_CONFIG, $MEDRE_HOME/config.yaml, "
                "saved selection, then standard discovery; "
                "see 'medre config current')"
            )
