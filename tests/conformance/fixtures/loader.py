"""Fixture loader for deterministic conformance JSON fixtures.

Loads JSON fixture files from ``tests/conformance/fixtures/<adapter>/``
and returns them as plain dicts.  Each fixture file includes:

- ``fixture_version``: schema version (currently ``1``).
- ``name``: human-readable fixture name.
- ``adapter``: adapter identifier (``"matrix"`` or ``"meshtastic"``).
- ``native_input``: the native dict payload consumed by the codec.
- ``decode_context``: extra kwargs passed to the codec decode method.
- ``expected``: assertions about the resulting CanonicalEvent.

Usage::

    from tests.conformance.fixtures.loader import load_fixture

    fx = load_fixture("matrix", "matrix_text_message")
    event = codec.decode(fx["native_input"], **fx["decode_context"])
    assert event.event_kind == fx["expected"]["event_kind"]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent


def load_fixture(adapter: str, name: str) -> dict[str, Any]:
    """Load a conformance fixture by adapter directory and filename stem.

    Parameters
    ----------
    adapter:
        Subdirectory under fixtures (e.g. ``"matrix"``, ``"meshtastic"``).
    name:
        Filename stem without ``.json`` extension
        (e.g. ``"matrix_text_message"``).

    Returns
    -------
    dict[str, Any]
        The parsed JSON fixture.

    Raises
    ------
    FileNotFoundError
        If the fixture file does not exist.
    ValueError
        If the fixture cannot be parsed as JSON.
    """
    path = _FIXTURES_DIR / adapter / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(
            f"Fixture {path} must be a JSON object, got {type(data).__name__}"
        )
    return data


def load_all_fixtures(adapter: str) -> list[dict[str, Any]]:
    """Load all JSON fixtures for a given adapter.

    Parameters
    ----------
    adapter:
        Subdirectory under fixtures (e.g. ``"matrix"``).

    Returns
    -------
    list[dict[str, Any]]
        All parsed fixtures sorted by filename.
    """
    adapter_dir = _FIXTURES_DIR / adapter
    if not adapter_dir.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for path in sorted(adapter_dir.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"Fixture {path} must be a JSON object, got {type(data).__name__}"
            )
        results.append(data)
    return results
