"""Matrix credential sidecar file helpers.

Provides functions for loading and resolving the Matrix credentials
JSON sidecar file (``$XDG_CONFIG_HOME/medre/credentials/matrix.json``).

These helpers are owned by the config layer because they deal with
credential resolution at config-time, not at runtime.  Concrete adapter
code may import them from here.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path


def get_credentials_path() -> Path:
    """Return the canonical path for the Matrix credentials JSON file.

    The path points to
    ``$XDG_CONFIG_HOME/medre/credentials/matrix.json``
    (defaulting to ``~/.config/medre/credentials/matrix.json``).

    This function does **not** create any directories.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(config_home) / "medre" / "credentials" / "matrix.json"


def load_credentials_json(path: Path | None = None) -> dict | None:
    """Load Matrix credentials JSON file.

    If *path* is provided, read from that path.  Otherwise use
    the default path from `get_credentials_path()`.

    Returns the credential dict, or ``None`` if the file does not exist or
    cannot be parsed.
    """
    target = path if path is not None else get_credentials_path()
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_credentials_json(data: Mapping[str, object], path: Path | None = None) -> Path:
    """Write Matrix credentials JSON file with restrictive permissions.

    If *path* is provided, write to that path.  Otherwise use
    the default path from `get_credentials_path()`.

    Creates parent directories if they don't exist.
    Returns the path that was written.
    """
    import stat

    target = path if path is not None else get_credentials_path()
    os.makedirs(target.parent, exist_ok=True)
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
    return target


__all__ = [
    "get_credentials_path",
    "load_credentials_json",
    "write_credentials_json",
]
