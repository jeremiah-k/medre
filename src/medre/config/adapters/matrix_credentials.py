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


def load_credentials_json() -> dict | None:
    """Load previously saved Matrix credentials.

    Returns the credential dict, or ``None`` if the file does not exist or
    cannot be parsed.
    """
    path = get_credentials_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


__all__ = [
    "get_credentials_path",
    "load_credentials_json",
]
