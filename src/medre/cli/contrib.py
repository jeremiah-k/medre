"""Isolated CLI contributions from installed adapter providers.

Adapter-specific parser and dispatch logic is owned by adapter packages and
referenced lazily through :mod:`medre.adapter_registry`.  The shared CLI owns
only the stable ``adapter`` namespace and contribution lifecycle.

Lazy-load invariant
-------------------
``medre --help`` may import lightweight contribution modules, but those modules
MUST NOT import optional transport SDKs. SDK-touching command implementations
remain lazy and execute only after the corresponding subcommand is selected.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from medre.adapter_registry import (
    get_adapter_spec,
    iter_adapter_specs,
    registered_transports,
)
from medre.core.observability.sanitization import sanitize_error

ALLOWED_NAMESPACES = ("adapter", "plugin")
DISALLOWED_TOPLEVEL = registered_transports()
_RESERVED_DESTINATIONS = {"command", "adapter_command", "_adapter_cli_failures"}


def _validate_parser_namespace(parser: argparse.ArgumentParser) -> None:
    """Prevent nested arguments/defaults from replacing core dispatch fields.

    argparse has no public action-tree iterator. Inspect its action/default
    tables here, including argument groups and recursively nested subparsers.
    """
    if _RESERVED_DESTINATIONS & parser._defaults.keys():
        raise ValueError("CLI defaults cannot replace core dispatch fields")
    for action in parser._actions:
        if action.dest in _RESERVED_DESTINATIONS:
            raise ValueError(f"CLI destination {action.dest!r} is reserved to core")
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                _validate_parser_namespace(child)


@dataclass(frozen=True, slots=True)
class CliContributionFailure:
    """A broken optional CLI hook, independent of runtime registration."""

    transport: str
    reason: str


def register_adapter_contributors(
    subparsers: Any,
) -> tuple[CliContributionFailure, ...]:
    """Build each contribution independently before exposing its command.

    Core owns the provider namespace. Hooks configure only their own parser,
    so failed/partial registrations cannot change another provider's commands.
    """
    loaded: list[tuple[str, argparse.ArgumentParser]] = []
    failures: list[CliContributionFailure] = []
    for spec in iter_adapter_specs():
        if spec.cli_register is None:
            continue
        hook = spec.cli_register
        try:
            register = hook.load()
            if spec.cli_dispatch is None:
                raise ValueError("CLI contribution requires a dispatch hook")
            hook = spec.cli_dispatch
            dispatch = hook.load()
            if not callable(register) or not callable(dispatch):
                raise TypeError("CLI register and dispatch hooks must be callable")
            hook = spec.cli_register
            parser = argparse.ArgumentParser(
                prog=f"medre adapter {spec.transport}",
                description=f"{spec.transport} adapter commands",
                allow_abbrev=False,
            )
            register(parser)
            _validate_parser_namespace(parser)
            # Formatting is part of registration: malformed descriptions or
            # formatters must not fail later while displaying generic help.
            parser.format_help()
        except Exception as exc:
            failures.append(
                CliContributionFailure(
                    spec.transport,
                    sanitize_error(
                        f"{hook.module}:{hook.name}: " f"{type(exc).__name__}: {exc}"
                    ),
                )
            )
        else:
            loaded.append((spec.transport, parser))
    if not loaded:
        return tuple(failures)

    adapter_parser = subparsers.add_parser(
        "adapter",
        help="Adapter management commands",
    )
    adapter_subparsers = adapter_parser.add_subparsers(
        dest="adapter_command",
        required=True,
    )
    for transport, parser in loaded:
        adapter_subparsers.add_parser(
            transport,
            parents=[parser],
            add_help=False,
            help=parser.description,
            description=parser.description,
            epilog=parser.epilog,
            usage=parser.usage,
            formatter_class=parser.formatter_class,
            allow_abbrev=False,
        )
    return tuple(failures)


def dispatch_contribution(args: Any) -> None:
    """Dispatch a contributed adapter command through its registered hook.

    Raises :class:`RuntimeError` when a registered parser has no dispatch hook
    or its hook declines the parsed command.
    """
    if getattr(args, "command", None) != "adapter":
        return

    transport = getattr(args, "adapter_command", None)
    if not isinstance(transport, str):
        return
    spec = get_adapter_spec(transport)
    if spec is None:
        return
    if spec.cli_dispatch is None:
        raise RuntimeError(
            f"Adapter CLI contributor for {transport!r} registered a parser "
            "but no dispatch hook"
        )

    dispatch = spec.cli_dispatch.load()
    handled = dispatch(args)
    if handled is False:
        raise RuntimeError(
            f"Adapter CLI contributor for {transport!r} did not handle its "
            "parsed command"
        )
