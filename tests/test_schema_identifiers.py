"""Schema identifier presence tests.

Asserts every ``*.schema.json`` under ``docs/schemas/`` declares both:

  * ``$schema`` — pinned to JSON Schema draft 2020-12.
  * ``$id``      — a stable ``https://medre.dev/schemas/<filename>`` URL.

These identifiers let YAML language servers and editor integrations resolve
schemas by URL (see ``docs/spec/configuration.md`` §3.5). A schema missing
either field breaks editor autocompletion and cross-references, so their
presence is guarded as a regression.

Split from ``tests/test_docs_schema_examples.py`` because that file is near
its 1,500-line ceiling; identifier metadata is an orthogonal concern.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _ROOT / "docs" / "schemas"

_DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
_ID_PREFIX = "https://medre.dev/schemas/"

#: Schemas that intentionally omit ``$id``/``$schema`` (none currently).
#: Add a stem here only with a justification in the docstring of the test.
_EXEMPT: frozenset[str] = frozenset()


def _schema_files() -> list[Path]:
    return sorted(_SCHEMAS_DIR.glob("*.schema.json"))


def test_every_schema_has_draft_2020_12_schema_field() -> None:
    """Each ``*.schema.json`` pins ``$schema`` to draft 2020-12."""
    for path in _schema_files():
        if path.stem in _EXEMPT:
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        declared = schema.get("$schema")
        assert declared == _DRAFT_2020_12, (
            f"{path.name}: $schema must be {_DRAFT_2020_12!r}, got {declared!r}"
        )


def test_every_schema_has_stable_id() -> None:
    """Each ``*.schema.json`` declares a ``$id`` under medre.dev."""
    for path in _schema_files():
        if path.stem in _EXEMPT:
            continue
        schema = json.loads(path.read_text(encoding="utf-8"))
        id_: Any = schema.get("$id")
        assert isinstance(id_, str), f"{path.name}: $id must be a string URL, got {id_!r}"
        assert id_.startswith(_ID_PREFIX), f"{path.name}: $id must start with {_ID_PREFIX!r}, got {id_!r}"
        # The $id basename must match the schema filename so resolution by
        # URL lands on the file with that name.
        assert (
            id_.rsplit("/", 1)[-1] == path.name
        ), f"{path.name}: $id basename {id_!r} does not match filename"


def test_schema_files_exist() -> None:
    """Guard against the glob silently matching nothing."""
    files = _schema_files()
    assert files, "No *.schema.json files found under docs/schemas/"
