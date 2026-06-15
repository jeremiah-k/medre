"""Matrix authentication helpers using only stdlib.

Provides login and whoami verification.  Login credentials are persisted via
``save_credentials_json`` (sidecar JSON), not by mutating the runtime config
file.  No dependency on nio or any Matrix SDK — uses ``urllib.request``
directly.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from medre.adapters.matrix.errors import MatrixConnectionError
from medre.config.adapters.matrix_credentials import write_credentials_json


def _normalize_homeserver(homeserver: str) -> str:
    """Normalize a Matrix homeserver URL.

    - Bare domain (no ``://``) → prepend ``https://``
    - Full ``https://`` URL → pass through unchanged
    - ``http://`` or any other scheme → raise :class:`ValueError`

    Leading/trailing whitespace is stripped before inspection.
    """
    homeserver = homeserver.strip()
    if "://" not in homeserver:
        return f"https://{homeserver}"
    if homeserver.startswith("https://"):
        return homeserver
    scheme = homeserver.split("://", 1)[0]
    raise ValueError(
        f"Unsupported scheme {scheme!r} for homeserver; "
        f"use 'https://…' or a bare domain"
    )


@dataclass(frozen=True)
class MatrixLoginResult:
    """Result of a successful Matrix password login."""

    homeserver: str
    user_id: str
    device_id: str
    access_token: str

    def __repr__(self) -> str:
        return (
            f"MatrixLoginResult(homeserver={self.homeserver!r}, "
            f"user_id={self.user_id!r}, "
            f"device_id={self.device_id!r}, "
            f"access_token='***')"
        )


def _redact_token(token: str, text: str) -> str:
    """Replace occurrences of *token* in *text* with ``***``."""
    if not token:
        return text
    return text.replace(token, "***")


def matrix_login(homeserver: str, user_id: str, password: str) -> MatrixLoginResult:
    """Authenticate with a Matrix homeserver using password login.

    POSTs to ``{homeserver}/_matrix/client/v3/login`` with
    ``m.login.password`` credentials.

    Parameters
    ----------
    homeserver:
        Base URL of the Matrix homeserver (e.g. ``"https://matrix.org"``).
        Bare domains (``"matrix.org"``) are accepted and automatically
        normalised to ``https://``.  ``http://`` and other non-HTTPS
        schemes raise :class:`ValueError`.
    user_id:
        Fully-qualified Matrix user ID (e.g. ``"@alice:matrix.org"``).
    password:
        The account password.

    Returns
    -------
    MatrixLoginResult
        Frozen dataclass with homeserver, user_id, device_id, access_token.

    Raises
    ------
    MatrixConnectionError
        On network failure, HTTP error, or missing fields in response.
    """
    homeserver = _normalize_homeserver(homeserver).rstrip("/")
    url = f"{homeserver}/_matrix/client/v3/login"
    payload = json.dumps(
        {
            "type": "m.login.password",
            "user": user_id,
            "password": password,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            req
        ) as resp:  # nosec: homeserver URL validated by _normalize_homeserver()
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _redact_token(password, raw)
        except Exception:
            pass
        finally:
            exc.close()
        raise MatrixConnectionError(
            f"Login failed (HTTP {exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MatrixConnectionError(
            f"Login failed (network error): {exc.reason}"
        ) from exc

    access_token = body.get("access_token")
    device_id = body.get("device_id")
    resolved_user_id = body.get("user_id")

    if not access_token:
        raise MatrixConnectionError("Login response missing access_token")
    if not resolved_user_id:
        raise MatrixConnectionError("Login response missing user_id")

    return MatrixLoginResult(
        homeserver=homeserver,
        user_id=resolved_user_id,
        device_id=device_id or "",
        access_token=access_token,
    )


def matrix_whoami(homeserver: str, access_token: str) -> str:
    """Verify an access token against the homeserver.

    GETs ``{homeserver}/_matrix/client/v3/account/whoami`` with a Bearer
    token and returns the resolved ``user_id``.

    Parameters
    ----------
    homeserver:
        Base URL of the Matrix homeserver.  Normalised the same way as
        in :func:`matrix_login` (bare domain → ``https://``, ``http://``
        rejected).
    access_token:
        The access token to verify.

    Returns
    -------
    str
        The ``user_id`` associated with the token.

    Raises
    ------
    MatrixConnectionError
        On network failure, HTTP error, or missing user_id.
    """
    homeserver = _normalize_homeserver(homeserver).rstrip("/")
    url = f"{homeserver}/_matrix/client/v3/account/whoami"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            req
        ) as resp:  # nosec: homeserver validated by _normalize_homeserver()
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _redact_token(access_token, raw)
        except Exception:
            pass
        finally:
            exc.close()
        raise MatrixConnectionError(
            f"Whoami failed (HTTP {exc.code}): {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MatrixConnectionError(
            f"Whoami failed (network error): {exc.reason}"
        ) from exc

    user_id = body.get("user_id")
    if not user_id:
        raise MatrixConnectionError("Whoami response missing user_id")

    return user_id


def extract_domain_from_mxid(user_id: str) -> str | None:
    """Extract the domain part from a Matrix user ID.

    Returns ``None`` when *user_id* is not a valid MXID (missing ``@``
    prefix or missing ``:`` separator).  Returns ``""`` when the domain
    portion is empty (e.g. ``"@a:"``).

    Examples::

        extract_domain_from_mxid("@bot:sk.community")  # "sk.community"
        extract_domain_from_mxid("bot")                # None
        extract_domain_from_mxid("")                   # None
        extract_domain_from_mxid("@a:")                # ""
    """
    if not user_id or ":" not in user_id:
        return None
    parts = user_id.split(":", 1)
    if not parts[0].startswith("@"):
        return None
    return parts[1]


def discover_well_known(domain: str) -> str | None:
    """Fetch ``/.well-known/matrix/client`` and return the homeserver base URL.

    Returns ``None`` on *any* error (network, HTTP, JSON decode, missing key,
    timeout).  No exception propagates to the caller.
    """
    url = f"https://{domain}/.well-known/matrix/client"
    try:
        with urllib.request.urlopen(
            url, timeout=5
        ) as resp:  # nosec: URL scheme is hardcoded to https://
            data = json.loads(resp.read())
        return data["m.homeserver"]["base_url"]  # type: ignore[index]
    except Exception:
        return None


def save_credentials_json(result: MatrixLoginResult, path: Path | None = None) -> Path:
    """Persist login credentials to disk with restrictive permissions (0o600).

    If *path* is provided, write to that *path*; otherwise use the
    default credentials path.

    Returns the :class:`Path` that was written.
    """
    return write_credentials_json(
        {
            "homeserver": result.homeserver,
            "access_token": result.access_token,
            "user_id": result.user_id,
            "device_id": result.device_id,
        },
        path=path,
    )


def check_credentials_completeness(creds: dict) -> list[str]:
    """Return a list of missing required credential keys.

    Required keys are ``"homeserver"``, ``"access_token"``, and
    ``"user_id"``.  The ``"device_id"`` key is optional.

    Only non-empty string values (truthy) count as present.  An empty
    return list means the credentials are complete.
    """
    required = ("homeserver", "access_token", "user_id")
    return [key for key in required if not creds.get(key)]
