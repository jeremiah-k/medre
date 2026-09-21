"""Hermetic environment isolation for configuration-loading tests.

Configuration outcomes depend on developer-machine state: ``MEDRE_HOME``
and ``MEDRE_CONFIG`` select config files, and XDG roots hold operator
state such as the Matrix credentials sidecar
(``$XDG_CONFIG_HOME/medre/credentials/matrix.json``), whose presence
silently changes validation results (see
``MatrixConfig._apply_sidecar_fallback`` — a local sidecar can make an
incomplete config validate while a CI runner without one rejects it).

Config-loading tests should consume :func:`isolated_config_env` (directly
or through a per-file autouse wrapper) so every test starts from a
pristine environment instead of inheriting the host's state.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

#: XDG roots redirected to fresh empty per-test directories.
ISOLATED_XDG_VARS: tuple[str, ...] = (
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)


@pytest.fixture
def isolated_config_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Mapping[str, Path]]:
    """Run one test against empty MEDRE/XDG configuration roots.

    Clears ``MEDRE_HOME`` and ``MEDRE_CONFIG`` and redirects every XDG
    root to a fresh empty directory, so config discovery and operator
    state (credentials sidecars, caches) cannot leak in from the host.

    Returns a mapping from environment variable name to its fresh root,
    so a test that needs to plant state (e.g. a credentials sidecar) can
    do so at ``roots["XDG_CONFIG_HOME"] / "medre" / ...``. Tests that
    need non-empty roots simply ``monkeypatch.setenv`` over these values;
    the fixture only guarantees the starting point is pristine.
    """
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    roots: dict[str, Path] = {}
    for name in ISOLATED_XDG_VARS:
        root = tmp_path / name.lower().replace("_", "-")
        root.mkdir()
        monkeypatch.setenv(name, str(root))
        roots[name] = root
    yield roots
