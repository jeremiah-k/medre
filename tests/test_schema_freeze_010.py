# medre-test-tier: compatibility
"""Freeze the exact published 0.1.0 schema artifacts before the first tag."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "docs" / "schemas"
FROZEN = json.loads((SCHEMA_DIR / "v0.1.0.freeze.json").read_text(encoding="utf-8"))


def test_first_release_schema_file_set_is_frozen() -> None:
    """Adding/removing an exported schema needs deliberate release review."""
    actual = {
        p.name
        for p in SCHEMA_DIR.iterdir()
        if p.name.endswith((".schema.json", ".schema.yaml", ".schema.sql"))
    }
    assert actual == set(FROZEN["schema_files_sha256"])


@pytest.mark.parametrize("name,sha256", sorted(FROZEN["schema_files_sha256"].items()))
def test_first_release_schema_content_is_frozen(name: str, sha256: str) -> None:
    """Changing a public schema requires an explicit supported-version decision."""
    assert hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest() == sha256


@pytest.mark.parametrize(
    "name",
    sorted(
        name for name in FROZEN["schema_files_sha256"] if name.endswith(".schema.json")
    ),
)
def test_frozen_json_schema_is_valid_draft_2020_12(name: str) -> None:
    """A matching checksum is insufficient if an initial public schema is invalid."""
    from jsonschema import Draft202012Validator

    schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)


def test_frozen_canonical_envelope_requires_identity_and_version() -> None:
    """The canonical v1 envelope must not accept omitted identity or bad versions."""
    from copy import deepcopy

    from jsonschema import Draft202012Validator

    schema = json.loads((SCHEMA_DIR / "canonical-event.schema.json").read_text())
    example = json.loads(
        (SCHEMA_DIR / "examples" / "canonical-event-example.json").read_text()
    )
    validator = Draft202012Validator(schema)
    validator.validate(example)
    for key in schema["required"]:
        missing = deepcopy(example)
        del missing[key]
        assert not validator.is_valid(missing), key
    invalid = deepcopy(example)
    invalid["schema_version"] = True
    assert not validator.is_valid(invalid)
