"""Auth CLI commands: matrix login."""
from __future__ import annotations

import getpass
import sys
from pathlib import Path


async def _auth_matrix_login(args: object) -> None:
    """Handle ``medre auth matrix login``.

    Reads password from stdin (``--password-stdin``) or :func:`getpass.getpass`,
    calls :func:`~medre.adapters.matrix.auth.matrix_login`,
    :func:`~medre.adapters.matrix.auth.matrix_whoami`, and
    :func:`~medre.adapters.matrix.auth.update_toml_credentials`.

    Never prints the access token.
    """
    # Lazy import to keep --help fast and SDK-free
    from medre.adapters.matrix.auth import (
        matrix_login,
        matrix_whoami,
        update_toml_credentials,
    )
    from medre.adapters.matrix.errors import MatrixConnectionError

    homeserver: str = args.homeserver  # type: ignore[attr-defined]
    user_id: str = args.user  # type: ignore[attr-defined]
    config_path = Path(args.config)  # type: ignore[attr-defined]
    adapter_name: str = args.adapter  # type: ignore[attr-defined]
    password_stdin: bool = getattr(args, "password_stdin", False)

    # Read password
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Matrix password: ")

    if not password:
        print("Error: password is required", file=sys.stderr)
        sys.exit(1)

    try:
        # Step 1: Login
        result = matrix_login(homeserver, user_id, password)

        # Step 2: Verify token with whoami
        whoami_user = matrix_whoami(result.homeserver, result.access_token)

        # Step 2b: Mismatch check
        if whoami_user != user_id:
            print(f"Error: verified user_id {whoami_user!r} does not match requested {user_id!r}", file=sys.stderr)
            sys.exit(1)

        # Step 3: Write credentials to config
        update_toml_credentials(
            config_path, "matrix", adapter_name,
            homeserver=result.homeserver,
            user_id=result.user_id,
            access_token=result.access_token,
        )

    except MatrixConnectionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # Step 4: Print results — never the token
    print(f"Homeserver: {result.homeserver}")
    print(f"User ID:    {whoami_user}")
    print(f"Device ID:  {result.device_id}")
    print(f"Config:     {config_path}")
    print(f"Adapter:    {adapter_name}")
    print()
    print("Token saved to config file.")
    print("Reminder: ensure your bot is joined to the required rooms.")
