"""Matrix authentication helpers using only stdlib.

Provides login, whoami verification, and config-file token update.
No dependency on nio or any Matrix SDK — uses ``urllib.request`` directly.
"""
from __future__ import annotations

import json
import os
import re
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from medre.adapters.matrix.errors import MatrixConnectionError


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


def _toml_escape_string(value: str) -> str:
    """Escape *value* for embedding in a TOML basic (double-quoted) string.

    Handles backslash, double quote, common whitespace escapes, and remaining
    control characters (U+0000–U+001F, U+007F) as ``\\uXXXX``.
    """
    result: list[str] = []
    for ch in value:
        if ch == "\\":
            result.append("\\\\")
        elif ch == '"':
            result.append('\\"')
        elif ch == "\n":
            result.append("\\n")
        elif ch == "\r":
            result.append("\\r")
        elif ch == "\t":
            result.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            result.append(f"\\u{ord(ch):04x}")
        else:
            result.append(ch)
    return "".join(result)


def matrix_login(homeserver: str, user_id: str, password: str) -> MatrixLoginResult:
    """Authenticate with a Matrix homeserver using password login.

    POSTs to ``{homeserver}/_matrix/client/v3/login`` with
    ``m.login.password`` credentials.

    Parameters
    ----------
    homeserver:
        Base URL of the Matrix homeserver (e.g. ``"https://matrix.org"``).
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
    homeserver = homeserver.rstrip("/")
    url = f"{homeserver}/_matrix/client/v3/login"
    payload = json.dumps({
        "type": "m.login.password",
        "user": user_id,
        "password": password,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _redact_token(password, raw)
        except Exception:
            pass
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
        raise MatrixConnectionError(
            "Login response missing access_token"
        )
    if not resolved_user_id:
        raise MatrixConnectionError(
            "Login response missing user_id"
        )

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
        Base URL of the Matrix homeserver.
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
    homeserver = homeserver.rstrip("/")
    url = f"{homeserver}/_matrix/client/v3/account/whoami"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = _redact_token(access_token, raw)
        except Exception:
            pass
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


def _update_toml_field(
    lines: list[str],
    config_path: Path,
    transport: str,
    adapter_name: str,
    field_name: str,
    value: str,
    *,
    redact: bool = False,
) -> tuple[list[str], bool]:
    """Replace a single TOML field value within a target adapter section.

    Parameters
    ----------
    lines:
        File content split into lines (with line endings preserved).
    config_path:
        Path to the config file (used in error messages only).
    transport:
        Transport type (e.g. ``"matrix"``).
    adapter_name:
        Adapter name within the transport group.
    field_name:
        TOML key to update (e.g. ``"access_token"``).
    value:
        New value for the field.
    redact:
        If *True*, the value is replaced with ``***`` in error messages.

    Returns
    -------
    tuple[list[str], bool]
        ``(new_lines, found)`` — *found* is ``True`` when the key was
        located and replaced in the target section.
    """
    section_header = f"[adapters.{transport}.{adapter_name}]"
    in_target_section = False
    found_key = False
    section_depth = 0

    escaped_field = re.escape(field_name)
    field_pattern_dq = re.compile(
        rf"^(\s*{escaped_field}\s*=\s*)\"[^\"]*\"(.*)$"
    )
    field_pattern_sq = re.compile(
        rf"^(\s*{escaped_field}\s*=\s*)'[^']*'(.*)$"
    )

    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if stripped.startswith("["):
            # Determine depth: count leading [ for [[...]] arrays
            bracket_count = 0
            for ch in stripped:
                if ch == "[":
                    bracket_count += 1
                else:
                    break

            current_section = stripped.lstrip("[").rstrip("]").strip()

            if current_section == f"adapters.{transport}.{adapter_name}":
                in_target_section = True
                section_depth = bracket_count
            else:
                # If we were in the target section and hit another section
                # at same or lower depth, we've left it
                if in_target_section:
                    in_target_section = False
                # Check if this is a deeper subsection of our target
                if current_section.startswith(
                    f"adapters.{transport}.{adapter_name}."
                ):
                    in_target_section = False
        else:
            # Blank line or non-section-header — stay in section context
            pass

        if in_target_section and not found_key:
            m = field_pattern_dq.match(line)
            if not m:
                m = field_pattern_sq.match(line)
            if m:
                line = (
                    f'{m.group(1)}"{_toml_escape_string(value)}"'
                    f"{m.group(2)}\n"
                )
                found_key = True

        new_lines.append(line)

    return new_lines, found_key


def _check_section_exists(lines: list[str], section_header: str) -> bool:
    """Return *True* if *section_header* (e.g. ``[adapters.matrix.bot]``)
    appears as a line in *lines*."""
    return any(line.strip() == section_header for line in lines)


def update_toml_access_token(
    config_path: Path,
    transport: str,
    adapter_name: str,
    token: str,
) -> None:
    """Update ``access_token`` in a TOML config file (line-level, preserves comments).

    Finds ``[adapters.{transport}.{adapter_name}]`` and replaces the
    ``access_token = "..."`` line within that section. Writes the file
    back with ``chmod 0600``.

    Parameters
    ----------
    config_path:
        Path to the TOML config file.
    transport:
        Transport type (e.g. ``"matrix"``).
    adapter_name:
        Adapter name within the transport group.
    token:
        New access token value.

    Raises
    ------
    FileNotFoundError
        If *config_path* does not exist.
    ValueError
        If the target section or ``access_token`` key is not found.
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}"
        )

    lines = config_path.read_text(encoding="utf-8").splitlines(True)

    lines, found = _update_toml_field(
        lines,
        config_path,
        transport,
        adapter_name,
        "access_token",
        token,
        redact=True,
    )

    if not found:
        section_header = f"[adapters.{transport}.{adapter_name}]"
        if not _check_section_exists(lines, section_header):
            raise ValueError(
                f"Section [{section_header[1:-1]}] not found in {config_path}"
            )
        raise ValueError(
            f"access_token key not found in "
            f"[adapters.{transport}.{adapter_name}] in {config_path}"
        )

    config_path.write_text("".join(lines), encoding="utf-8")
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def update_toml_credentials(
    config_path: Path,
    transport: str,
    adapter_name: str,
    *,
    homeserver: str,
    user_id: str,
    access_token: str,
) -> None:
    """Update ``homeserver``, ``user_id``, and ``access_token`` in a TOML
    config file (line-level, preserves comments).

    Reads the file once, applies all three field replacements, and writes
    once.  The file is set to ``chmod 0600`` after writing.

    Parameters
    ----------
    config_path:
        Path to the TOML config file.
    transport:
        Transport type (e.g. ``"matrix"``).
    adapter_name:
        Adapter name within the transport group.
    homeserver:
        New homeserver URL.
    user_id:
        New Matrix user ID.
    access_token:
        New access token value.

    Raises
    ------
    FileNotFoundError
        If *config_path* does not exist.
    ValueError
        If the target section or any of the three keys is not found.
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}"
        )

    lines = config_path.read_text(encoding="utf-8").splitlines(True)

    field_updates: list[tuple[str, str, bool]] = [
        ("homeserver", homeserver, False),
        ("user_id", user_id, False),
        ("access_token", access_token, True),
    ]

    section_header = f"[adapters.{transport}.{adapter_name}]"

    for field_name, value, redact in field_updates:
        lines, found = _update_toml_field(
            lines,
            config_path,
            transport,
            adapter_name,
            field_name,
            value,
            redact=redact,
        )
        if not found:
            if not _check_section_exists(lines, section_header):
                raise ValueError(
                    f"Section [{section_header[1:-1]}] not found"
                    f" in {config_path}"
                )
            display_val = "***" if redact else value
            raise ValueError(
                f"{field_name} key not found in "
                f"[adapters.{transport}.{adapter_name}] in {config_path}"
            )

    config_path.write_text("".join(lines), encoding="utf-8")
    config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0o600
