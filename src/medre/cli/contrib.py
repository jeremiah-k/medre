"""MEDRE CLI contribution dispatch for built-in adapters.

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

from typing import Any

from medre.adapter_registry import (
    get_adapter_spec,
    iter_adapter_specs,
    registered_transports,
)

ALLOWED_NAMESPACES = ("adapter", "plugin")
DISALLOWED_TOPLEVEL = registered_transports()


def register_builtin_contributors(subparsers: Any) -> None:
    """Register built-in adapter CLI contributors in registry order."""
    contributors = [
        spec for spec in iter_adapter_specs() if spec.cli_register is not None
    ]
    if not contributors:
        return

    adapter_parser = subparsers.add_parser(
        "adapter",
        help="Adapter management commands",
    )
    adapter_subparsers = adapter_parser.add_subparsers(
        dest="adapter_command",
        required=True,
    )
    for spec in contributors:
        register = spec.cli_register.load()
        register(adapter_subparsers)


def dispatch_contribution(args: Any) -> None:
    """Dispatch a contributed adapter command through its registered hook."""
    if getattr(args, "command", None) != "adapter":
        return

    transport = getattr(args, "adapter_command", None)
    if not isinstance(transport, str):
        return
    spec = get_adapter_spec(transport)
    if spec is None or spec.cli_dispatch is None:
        return

    dispatch = spec.cli_dispatch.load()
    handled = dispatch(args)
    if handled is False:
        raise RuntimeError(
            f"Adapter CLI contributor for {transport!r} did not handle its "
            "parsed command"
        )
