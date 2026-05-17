"""MEDRE CLI command-contribution registry.

Adapter-specific subcommands (adapter, plugin) are registered here
instead of :pymod:`medre.cli.main` so that the core parser never imports
optional SDK packages.  Only the registration and dispatch plumbing lives in
this module; actual command logic is lazy-imported inside dispatch branches.

Namespace rules
---------------
- **Allowed top-level namespaces:** ``adapter``, ``plugin``.
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

import argparse

ALLOWED_NAMESPACES = ("adapter", "plugin")
DISALLOWED_TOPLEVEL = ("matrix", "meshtastic", "lxmf", "meshcore")


def register_builtin_contributors(subparsers) -> None:
    """Register all built-in command contributors in deterministic order."""
    _register_matrix_contributions(subparsers)


def _register_matrix_contributions(subparsers) -> None:
    # -- adapter namespace ----------------------------------------------------
    adapter_p = subparsers.add_parser("adapter", help="Adapter management commands")
    adapter_sub = adapter_p.add_subparsers(dest="adapter_command", required=True)

    # -- adapter matrix -------------------------------------------------------
    adapter_matrix_p = adapter_sub.add_parser(
        "matrix", help="Matrix transport adapter commands",
    )
    adapter_matrix_sub = adapter_matrix_p.add_subparsers(
        dest="adapter_matrix_command", required=True,
    )

    # -- adapter matrix auth --------------------------------------------------
    adapter_matrix_auth_p = adapter_matrix_sub.add_parser(
        "auth", help="Matrix credential setup (no runtime). Mutates config file. Writes homeserver, user_id, access_token. Never prints token. Prompts for password securely.",
    )
    adapter_matrix_auth_sub = adapter_matrix_auth_p.add_subparsers(
        dest="adapter_matrix_auth_command", required=True,
    )

    # -- adapter matrix auth status -------------------------------------------
    adapter_matrix_auth_sub.add_parser(
        "status", help="Show Matrix credential file status without printing secrets",
    )

    # -- adapter matrix auth login --------------------------------------------
    auth_login_p = adapter_matrix_auth_sub.add_parser(
        "login",
        help="Authenticate with homeserver, verify token, write credentials to config. Never prints the access token.",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  (a) Interactive (no flags — prompts for homeserver, user, and password):
    medre adapter matrix auth login \\
      --config ~/.config/medre/config.toml \\
      --adapter-id mybot

  (b) Non-interactive (all flags):
    medre adapter matrix auth login \\
      --config ~/.config/medre/config.toml \\
      --adapter-id mybot \\
      --homeserver matrix.example.com \\
      --user @bot:example.com \\
      --password 'your_password'

  (c) MXID derivation (localpart only — derives @localpart:homeserver):
    medre adapter matrix auth login \\
      --config ~/.config/medre/config.toml \\
      --adapter-id mybot \\
      --homeserver matrix.example.com \\
      --user bot
""",
    )
    auth_login_p.add_argument(
        "--config", required=True, help="Path to config file to update",
    )
    auth_login_p.add_argument(
        "--adapter-id", dest="adapter_id", required=True,
        help="Adapter instance ID in config (e.g. 'matrix')",
    )
    auth_login_p.add_argument(
        "--homeserver", required=False, default=None,
        help="Homeserver URL or bare domain (e.g. 'matrix.example.com')",
    )
    auth_login_p.add_argument(
        "--user", required=False, default=None,
        help="User ID (e.g. @bot:example.com) or localpart for MXID derivation",
    )
    auth_login_p.add_argument(
        "--password", required=False, default=None,
        help="Password for non-interactive mode",
    )
    auth_login_p.add_argument(
        "--password-stdin", action="store_true", default=False,
        help="Read password from stdin instead of interactive prompt",
    )


def dispatch_contribution(args) -> None:
    """Dispatch a contributed command, lazy-importing only when needed."""
    if (
        args.command == "adapter"
        and getattr(args, "adapter_command", None) == "matrix"
        and getattr(args, "adapter_matrix_command", None) == "auth"
        and getattr(args, "adapter_matrix_auth_command", None) == "status"
    ):
        from medre.adapters.matrix.cli import _adapter_matrix_auth_status

        import asyncio

        asyncio.run(_adapter_matrix_auth_status())
    elif (
        args.command == "adapter"
        and getattr(args, "adapter_command", None) == "matrix"
        and getattr(args, "adapter_matrix_command", None) == "auth"
        and getattr(args, "adapter_matrix_auth_command", None) == "login"
    ):
        from medre.adapters.matrix.cli import _adapter_matrix_auth_login

        import asyncio

        asyncio.run(_adapter_matrix_auth_login(args))
