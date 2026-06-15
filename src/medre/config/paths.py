"""XDG-compatible path resolution for MEDRE.

:func:`resolve` is the public entry-point.  It reads environment variables
once and returns an immutable :class:`MedrePaths` instance that holds every
directory and file path the application needs at runtime.

Two resolution modes are supported:

* **XDG mode** (default) — honours the XDG Base Directory Specification.
  Each path category (config, state, data, cache) is resolved independently
  against its corresponding ``XDG_*_HOME`` variable or the spec-defined
  fallback.
* **MEDRE_HOME mode** — when the ``MEDRE_HOME`` environment variable is set
  to a non-empty value, *all* paths are resolved relative to that single
  directory.  This is intended for containers, Docker, Kubernetes, and local
  development where a unified data layout is preferred.

**Important:** this module performs *pure path resolution only*.  No
directories are created.  Callers are responsible for ensuring directories
exist before use.

References
----------
XDG Base Directory Specification:
    https://specifications.freedesktop.org/basedir-spec/latest/
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["MedrePaths", "MedrePathsError", "resolve"]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

_VALID_PLACEHOLDERS: frozenset[str] = frozenset(
    {"config", "state", "data", "cache", "logs"}
)


class MedrePathsError(Exception):
    """Raised for path-resolution or placeholder-expansion errors."""


# ---------------------------------------------------------------------------
# Immutable paths container
# ---------------------------------------------------------------------------

_CONFIG_FILENAME: str = "config.yaml"
_APP_SUBDIR: str = "medre"


@dataclass(frozen=True)
class MedrePaths:
    """Immutable, fully-resolved path layout for MEDRE.

    Use the :func:`resolve` factory to obtain an instance — do **not**
    construct one directly.

    Attributes
    ----------
    config_dir:
        Configuration directory (XDG mode only).  ``None`` when
        ``MEDRE_HOME`` is set because that mode uses a single config file
        rather than a directory.
    config_file:
        Absolute path to the main YAML configuration file.
    state_dir:
        Directory for mutable application state.
    data_dir:
        Directory for persistent application data.
    cache_dir:
        Directory for disposable cached data.
    log_dir:
        Directory for log files.
    database_path:
        Absolute path to the SQLite database file.
    """

    config_dir: Path | None
    config_file: Path
    state_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    database_path: Path

    # -- Derived paths -------------------------------------------------------

    def adapter_state_dir(self, adapter_id: str) -> Path:
        """Return the state directory for *adapter_id*.

        Parameters
        ----------
        adapter_id:
            Adapter identifier used as a subdirectory name.

        Returns
        -------
        Path
            ``state_dir / "adapters" / {adapter_id}``

        Raises
        ------
        MedrePathsError
            If *adapter_id* is empty or contains path separators.
        """
        if not adapter_id:
            raise MedrePathsError("adapter_id must be non-empty")
        if os.sep in adapter_id or (os.altsep and os.altsep in adapter_id):
            raise MedrePathsError(
                f"adapter_id must not contain path separators: {adapter_id!r}"
            )
        return self.state_dir / "adapters" / adapter_id

    def adapter_transport_state_dir(self, adapter_id: str, transport: str) -> Path:
        """Return the transport-specific state directory for *adapter_id*.

        Parameters
        ----------
        adapter_id:
            Adapter identifier used as a subdirectory name.
        transport:
            Transport name (e.g. ``"matrix"``, ``"lxmf"``).

        Returns
        -------
        Path
            ``state_dir / "adapters" / {adapter_id} / {transport}``

        Raises
        ------
        MedrePathsError
            If *adapter_id* or *transport* is empty or contains path
            separators.
        """
        if not transport:
            raise MedrePathsError("transport must be non-empty")
        if os.sep in transport or (os.altsep and os.altsep in transport):
            raise MedrePathsError(
                f"transport must not contain path separators: {transport!r}"
            )
        return self.adapter_state_dir(adapter_id) / transport

    # -- Placeholder expansion ------------------------------------------------

    def expand_placeholder(self, value: str) -> Path:
        """Expand known ``{name}`` placeholders in *value*.

        Supported placeholders:

        ==========  ================================================
        Placeholder  Resolves to
        ==========  ================================================
        ``{config}``  :attr:`config_dir` (or ``config_file.parent``
                      when ``MEDRE_HOME`` is active)
        ``{state}``   :attr:`state_dir`
        ``{data}``    :attr:`data_dir`
        ``{cache}``   :attr:`cache_dir`
        ``{logs}``    :attr:`log_dir`
        ==========  ================================================

        Parameters
        ----------
        value:
            A string potentially containing ``{name}`` placeholders.

        Returns
        -------
        Path
            The fully-expanded absolute path.

        Raises
        ------
        MedrePathsError
            If *value* contains an unrecognised placeholder.
        """
        config_root = (
            self.config_dir if self.config_dir is not None else self.config_file.parent
        )

        placeholder_map: dict[str, Path] = {
            "config": config_root,
            "state": self.state_dir,
            "data": self.data_dir,
            "cache": self.cache_dir,
            "logs": self.log_dir,
        }

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in _VALID_PLACEHOLDERS:
                raise MedrePathsError(f"unknown path placeholder: {{{name}}}")
            return str(placeholder_map[name])

        expanded = _PLACEHOLDER_RE.sub(_replace, value)
        return Path(expanded)

    # -- Diagnostics ----------------------------------------------------------

    def to_diagnostics(self) -> dict[str, str]:
        """Return a string-valued snapshot of all paths for logging.

        The returned mapping contains **no secrets** — only filesystem
        paths serialised as strings.
        """
        return {
            "config_dir": str(self.config_dir) if self.config_dir else "(none)",
            "config_file": str(self.config_file),
            "state_dir": str(self.state_dir),
            "data_dir": str(self.data_dir),
            "cache_dir": str(self.cache_dir),
            "log_dir": str(self.log_dir),
            "database_path": str(self.database_path),
            "adapter_state_root": str(self.state_dir / "adapters"),
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _xdg_dir(xdg_var: str, default_suffix: str) -> Path:
    """Resolve a single XDG base directory.

    Parameters
    ----------
    xdg_var:
        Environment variable name (e.g. ``"XDG_CONFIG_HOME"``).
    default_suffix:
        Fallback path *relative to the user's home directory*
        (e.g. ``".config"``).
    """
    value = os.environ.get(xdg_var)
    if value:
        return Path(value)
    return Path.home() / default_suffix


def resolve() -> MedrePaths:
    """Read environment variables and return an immutable :class:`MedrePaths`.

    Resolution rules:

    * If ``MEDRE_HOME`` is set **and non-empty**, all paths are derived from
      that single root directory.
    * Otherwise the XDG Base Directory Specification is followed.

    This function performs **no I/O** — it only resolves paths.
    """
    medre_home_raw = os.environ.get("MEDRE_HOME", "")
    medre_home = medre_home_raw.strip()

    if medre_home:
        home = Path(medre_home)
        state_dir = home / "state"
        return MedrePaths(
            config_dir=None,
            config_file=home / _CONFIG_FILENAME,
            state_dir=state_dir,
            data_dir=home / "data",
            cache_dir=home / "cache",
            log_dir=home / "logs",
            database_path=state_dir / "medre.sqlite",
        )

    # XDG mode
    config_dir = _xdg_dir("XDG_CONFIG_HOME", ".config") / _APP_SUBDIR
    state_dir = _xdg_dir("XDG_STATE_HOME", ".local/state") / _APP_SUBDIR
    return MedrePaths(
        config_dir=config_dir,
        config_file=config_dir / _CONFIG_FILENAME,
        state_dir=state_dir,
        data_dir=_xdg_dir("XDG_DATA_HOME", ".local/share") / _APP_SUBDIR,
        cache_dir=_xdg_dir("XDG_CACHE_HOME", ".cache") / _APP_SUBDIR,
        log_dir=state_dir / "logs",
        database_path=state_dir / "medre.sqlite",
    )
