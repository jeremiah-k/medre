"""Matrix adapter CLI parser/dispatch contribution.

This module is intentionally SDK-free at import time so ``medre --help`` can
load the contribution without importing nio.  Command implementations remain
lazy imports from :mod:`medre.adapters.matrix.cli`.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

__all__ = ["dispatch_matrix_cli", "register_matrix_cli"]


def register_matrix_cli(adapter_subparsers: Any) -> None:
    """Register ``medre adapter matrix ...`` below the adapter namespace."""
    adapter_matrix_p = adapter_subparsers.add_parser(
        "matrix",
        help="Matrix transport adapter commands",
    )
    adapter_matrix_sub = adapter_matrix_p.add_subparsers(
        dest="adapter_matrix_command",
        required=True,
    )

    adapter_matrix_auth_p = adapter_matrix_sub.add_parser(
        "auth",
        help=(
            "Matrix credential and E2EE identity setup (no runtime). Writes the "
            "credentials sidecar and never prints tokens or passwords."
        ),
    )
    adapter_matrix_auth_sub = adapter_matrix_auth_p.add_subparsers(
        dest="adapter_matrix_auth_command",
        required=True,
    )

    adapter_matrix_auth_sub.add_parser(
        "status",
        help="Show Matrix credential file status without printing secrets",
    )

    auth_login_p = adapter_matrix_auth_sub.add_parser(
        "login",
        help=(
            "Authenticate with homeserver, verify token, save credentials to "
            "sidecar file. Never prints the access token."
        ),
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  Interactive (no flags — prompts for user ID and password, derives\n"
            "  homeserver from MXID, does well-known discovery). Preferred for\n"
            "  interactive operators:\n"
            "    medre adapter matrix auth login\n"
            "\n"
            "  Non-interactive via stdin (preferred for automation; password is\n"
            "  read from stdin and never appears in shell history or process\n"
            "  listings). Redirect from a file or pipe from a secret manager:\n"
            "    medre adapter matrix auth login \\\n"
            "      --homeserver matrix.example.com \\\n"
            "      --user @bot:example.com \\\n"
            "      --password-stdin < /run/secrets/matrix_password\n"
            "\n"
            "  With MXID derivation (homeserver optional):\n"
            "    medre adapter matrix auth login \\\n"
            "      --user @bot:example.com\n"
            "\n"
            "  Prepare the E2EE store and cross-sign the runtime device. The\n"
            "  adapter ID must match the configured Matrix adapter key:\n"
            "    medre adapter matrix auth login \\\n"
            "      --user @bot:example.com --adapter-id main \\\n"
            "      --password-stdin < /run/secrets/matrix_password\n"
            "\n"
            "  Destructive recovery is explicit and password-authenticated:\n"
            "    medre adapter matrix auth login \\\n"
            "      --user @bot:example.com --adapter-id main \\\n"
            "      --reset-cross-signing \\\n"
            "      --password-stdin < /run/secrets/matrix_password\n"
            "\n"
            "  --password reads the password from the command line. This is\n"
            "  supported for automation that cannot pipe stdin, but the value is\n"
            "  visible in shell history, process listings, and audit logs; prefer\n"
            "  --password-stdin whenever the caller can read from a file or pipe.\n"
            "\n"
            "Credentials are saved to a sidecar JSON file. No config file required.\n"
        ),
    )
    auth_login_p.add_argument(
        "--homeserver",
        required=False,
        default=None,
        help="Homeserver URL or bare domain (e.g. 'matrix.example.com')",
    )
    auth_login_p.add_argument(
        "--user",
        required=False,
        default=None,
        help="User ID (e.g. @bot:example.com) or localpart for MXID derivation",
    )
    auth_login_p.add_argument(
        "--password",
        required=False,
        default=None,
        help="Password for non-interactive mode",
    )
    auth_login_p.add_argument(
        "--password-stdin",
        action="store_true",
        default=False,
        help="Read password from stdin instead of interactive prompt",
    )
    auth_login_p.add_argument(
        "--adapter-id",
        required=False,
        default=None,
        help=(
            "Prepare cross-signing state in this Matrix adapter's runtime E2EE "
            "store (must match the adapter key in config)"
        ),
    )
    auth_login_p.add_argument(
        "--reset-cross-signing",
        action="store_true",
        default=False,
        help=(
            "Explicitly replace conflicting/lost cross-signing identity material; "
            "requires --adapter-id and fresh password authentication"
        ),
    )

    provision_p = adapter_matrix_sub.add_parser(
        "provision",
        help=(
            "Provision one private space + one private encrypted room linked to "
            "it, invite users to both, and pre-assign admin power (effective on "
            "join). Verifies encryption/linkage/power from server state. "
            "Requires 'adapter matrix auth login' first. Prints room/space IDs "
            "(IDs are not credentials)."
        ),
        allow_abbrev=False,
    )
    provision_p.add_argument(
        "--space-name",
        required=True,
        help="Name for the private test space",
    )
    provision_p.add_argument(
        "--room-name",
        required=True,
        help="Name for the private encrypted test room",
    )
    provision_p.add_argument(
        "--room-topic",
        required=False,
        default=None,
        help="Optional topic for the encrypted room",
    )
    provision_p.add_argument(
        "--invite",
        action="append",
        required=True,
        metavar="USER_ID",
        help="User ID to invite to BOTH resources (repeatable)",
    )
    provision_p.add_argument(
        "--admin",
        action="append",
        required=False,
        default=None,
        metavar="USER_ID",
        help=(
            "Invited user to pre-assign admin power 100 in BOTH resources "
            "(effective on join; repeatable)"
        ),
    )


def dispatch_matrix_cli(args: Any) -> bool:
    """Dispatch a parsed Matrix adapter command.

    Returns ``True`` when the arguments belong to a Matrix command, otherwise
    ``False``.  The generic CLI dispatcher uses the return value to detect a
    malformed contribution without knowing Matrix's subcommand vocabulary.
    """
    if getattr(args, "adapter_command", None) != "matrix":
        return False

    command = getattr(args, "adapter_matrix_command", None)
    if command == "auth":
        auth_command = getattr(args, "adapter_matrix_auth_command", None)
        if auth_command == "status":
            from medre.adapters.matrix.cli import _adapter_matrix_auth_status

            asyncio.run(_adapter_matrix_auth_status())
            return True
        if auth_command == "login":
            from medre.adapters.matrix.cli import _adapter_matrix_auth_login

            asyncio.run(_adapter_matrix_auth_login(args))
            return True
        return False

    if command == "provision":
        from medre.adapters.matrix.cli import _adapter_matrix_provision

        asyncio.run(_adapter_matrix_provision(args))
        return True

    return False
