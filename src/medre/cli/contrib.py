"""MEDRE CLI command-contribution registry.

Adapter-specific subcommands (auth, adapter, plugin) are registered here
instead of :pymod:`medre.cli.main` so that the core parser never imports
optional SDK packages.  Only the registration and dispatch plumbing lives in
this module; actual command logic is lazy-imported inside dispatch branches.

Namespace rules
---------------
- **Allowed top-level namespaces:** ``auth``, ``adapter``, ``plugin``.
- **Disallowed top-level names:** transport names (``matrix``,
  ``meshtastic``, ``lxmf``, ``meshcore``) must never appear as a
  top-level command — they belong under a namespace.

Lazy-load invariant
-------------------
``medre --help`` must not import any optional SDK (nio, meshtastic, RNS,
LXMF).  All SDK-touching imports happen inside dispatch branches that only
execute when the user explicitly invokes the corresponding subcommand.
"""

from __future__ import annotations

ALLOWED_NAMESPACES = ("auth", "adapter", "plugin")
DISALLOWED_TOPLEVEL = ("matrix", "meshtastic", "lxmf", "meshcore")


def register_builtin_contributors(subparsers) -> None:
    """Register all built-in command contributors in deterministic order."""
    _register_auth_matrix_login(subparsers)


def _register_auth_matrix_login(subparsers) -> None:
    # -- auth namespace -------------------------------------------------------
    auth_p = subparsers.add_parser("auth", help="Authentication commands")
    auth_sub = auth_p.add_subparsers(dest="auth_command", required=True)

    # -- auth matrix ----------------------------------------------------------
    auth_matrix_p = auth_sub.add_parser(
        "matrix", help="Matrix transport auth",
    )
    auth_matrix_sub = auth_matrix_p.add_subparsers(
        dest="auth_matrix_command", required=True,
    )

    # -- auth matrix login ----------------------------------------------------
    auth_login_p = auth_matrix_sub.add_parser(
        "login", help="Login and store Matrix access token",
    )
    auth_login_p.add_argument(
        "--config", required=True, help="Path to config file to update",
    )
    auth_login_p.add_argument(
        "--adapter", required=True,
        help="Adapter instance name in config (e.g. 'matrix')",
    )
    auth_login_p.add_argument(
        "--homeserver", required=True, help="Homeserver URL",
    )
    auth_login_p.add_argument(
        "--user", required=True,
        help="User ID (e.g. @bot:example.com)",
    )
    auth_login_p.add_argument(
        "--password-stdin", action="store_true", default=False,
        help="Read password from stdin instead of interactive prompt",
    )


def dispatch_contribution(args) -> None:
    """Dispatch a contributed command, lazy-importing only when needed."""
    if (
        args.command == "auth"
        and getattr(args, "auth_command", None) == "matrix"
        and getattr(args, "auth_matrix_command", None) == "login"
    ):
        from .auth_commands import _auth_matrix_login

        import asyncio

        asyncio.run(_auth_matrix_login(args))
