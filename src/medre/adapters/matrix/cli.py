"""Matrix adapter CLI commands: auth login, auth status."""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path


async def _adapter_matrix_auth_login(args: object) -> None:
    """Handle ``medre adapter matrix auth login``.

    Tristate dispatch:

    - **Fully interactive** — no auth flags given.  Prompts for user ID and
      password, derives homeserver from the MXID.
    - **Non-interactive** — ``--user``, ``--password`` (or ``--password-stdin``)
      all present.  ``--homeserver`` is optional when ``--user`` is a full MXID.
    - **Partial** — some flags given but incomplete.  Prints guidance and exits.

    Never prints the access token.
    """
    # Step 1: Read args via getattr (all optional now) -----------------------
    homeserver = getattr(args, "homeserver", None)
    user_id = getattr(args, "user", None)
    password_cli = getattr(args, "password", None)
    password_stdin: bool = getattr(args, "password_stdin", False)

    # Step 2: Tristate dispatch ----------------------------------------------
    # Count how many auth-relevant flags were explicitly provided.
    provided = sum(
        [
            homeserver is not None,
            user_id is not None,
            password_cli is not None,
            password_stdin,
        ]
    )

    password: str | None = None

    if provided == 0:
        # --- FULLY INTERACTIVE ----------------------------------------------
        user_id = input("Matrix user ID (e.g. @bot:example.com): ").strip()
        if not user_id:
            print("Error: user ID required", file=sys.stderr)
            sys.exit(1)
        password = getpass.getpass("Matrix password: ")
        if not password:
            print("Error: password is required", file=sys.stderr)
            sys.exit(1)

    elif provided > 0:
        # Check whether we have enough for non-interactive mode.
        # A full MXID counts as both user AND homeserver (derivable).
        has_password = password_cli is not None or password_stdin

        # Determine if user_id is a full MXID (contains ':')
        user_is_mxid = (
            user_id is not None and user_id.startswith("@") and ":" in user_id
        )

        # Homeserver is available if explicitly given or derivable from MXID
        has_homeserver = homeserver is not None or user_is_mxid
        has_user = user_id is not None

        if has_user and has_password and has_homeserver:
            # --- NON-INTERACTIVE (all required present) ---------------------
            pass
        else:
            # --- PARTIAL — give guidance ------------------------------------
            missing: list[str] = []
            if not has_user:
                missing.append("--user")
            if not has_password:
                missing.append("--password (or --password-stdin)")
            if not has_homeserver:
                missing.append(
                    "--homeserver (or provide a full MXID like @bot:example.com)"
                )
            print(
                f"Error: incomplete credentials. Missing: {', '.join(missing)}",
                file=sys.stderr,
            )
            print(
                "Provide all flags for non-interactive mode, "
                "or run with no flags for interactive login.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Step 3 (interactive password already acquired above)

    # Step 4: Homeserver derivation ------------------------------------------
    assert user_id is not None  # guaranteed by tristate dispatch above
    if homeserver is None:
        from medre.adapters.matrix.auth import (
            discover_well_known,
            extract_domain_from_mxid,
        )

        domain = extract_domain_from_mxid(user_id)
        if domain is None:
            print(
                "Cannot derive homeserver. Use --homeserver or provide "
                "a full MXID (@user:server).",
                file=sys.stderr,
            )
            sys.exit(1)

        discovered = discover_well_known(domain)
        if discovered:
            homeserver = discovered
            print(f"Resolved homeserver via .well-known: {homeserver}")
        else:
            homeserver = f"https://{domain}"
            print(f"Could not reach .well-known; using {homeserver}")

    # Step 5: Password acquisition -------------------------------------------
    if password is not None:
        # Already set (interactive mode)
        pass
    elif password_cli:
        password = password_cli
    elif password_stdin:
        if os.isatty(sys.stdin.fileno()):
            print(
                "Error: --password-stdin expects piped input. "
                "Use interactive login (without --password-stdin), "
                "or pipe the password.",
                file=sys.stderr,
            )
            sys.exit(1)
        password = sys.stdin.readline().rstrip("\n")

    if not password:
        print("Error: password is required", file=sys.stderr)
        sys.exit(1)

    # Step 6: Login + whoami + mismatch check --------------------------------
    from medre.adapters.matrix.auth import (
        MatrixConnectionError,
        matrix_login,
        matrix_whoami,
        save_credentials_json,
    )

    try:
        result = matrix_login(homeserver, user_id, password)

        whoami_user = matrix_whoami(result.homeserver, result.access_token)

        if whoami_user != user_id:
            print(
                f"Error: verified user_id {whoami_user!r} does not match "
                f"requested {user_id!r}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Step 7: Save credentials to sidecar JSON (always)
        creds_path = save_credentials_json(result)

    except MatrixConnectionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 8: Print results — never the access token -------------------------
    print(f"Homeserver:  {result.homeserver}")
    print(f"User ID:     {result.user_id}")
    print(f"Device ID:   {result.device_id}")
    print(f"Credentials: {creds_path}")


async def _adapter_matrix_auth_status(credentials_path: Path | None = None) -> None:
    """Handle ``medre adapter matrix auth status``.

    Shows whether a credentials sidecar JSON exists, which fields are present,
    and reports missing required keys.  Never prints the access token value.

    Parameters
    ----------
    credentials_path:
        Optional explicit path to the credentials file.  When omitted
        the default sidecar path is used.
    """
    from medre.adapters.matrix.auth import check_credentials_completeness
    from medre.config.adapters.matrix_credentials import (
        get_credentials_path,
        load_credentials_json,
    )

    path = credentials_path if credentials_path is not None else get_credentials_path()

    if not path.exists():
        print(f"No credentials file at: {path}")
        print("Run 'medre adapter matrix auth login' to authenticate.")
        return

    creds = load_credentials_json(path=path)
    if creds is None:
        print(f"Credentials file malformed: {path}")
        return

    missing = check_credentials_completeness(creds)

    print(f"Credentials:  {path}")
    print(
        f"Homeserver:   "
        f"{'✓ ' + str(creds.get('homeserver', '')) if creds.get('homeserver') else '✗ missing'}"
    )
    print(
        f"User ID:      "
        f"{'✓ ' + str(creds.get('user_id', '')) if creds.get('user_id') else '✗ missing'}"
    )
    print(
        f"Device ID:    "
        f"{'✓ ' + str(creds.get('device_id', '')) if creds.get('device_id') else '— (optional)'}"
    )
    print(
        f"Access token: "
        f"{'✓ (present)' if creds.get('access_token') else '✗ missing'}"
    )

    if missing:
        print(f"\nMissing: {', '.join(missing)}")
    else:
        print("\nCredentials are complete.")
