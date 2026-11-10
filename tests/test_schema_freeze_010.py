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


def test_canonical_json_and_yaml_mirrors_have_identical_validation_semantics() -> None:
    """Doc wording may differ, but the two frozen forms must enforce one shape."""
    import yaml

    # Values of these keys map member names to subschemas, so a member may
    # itself be named "description" and only the subschemas are searched.
    named_subschemas = {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependencies",
    }
    # Values of these keys are instance data rather than schemas.
    instance_data = {"const", "default", "enum", "examples", "dependentRequired"}

    def without_descriptions(value: object) -> object:
        if isinstance(value, dict):
            stripped = {}
            for key, item in value.items():
                if key == "description":
                    continue
                if key in instance_data:
                    stripped[key] = item
                elif key in named_subschemas and isinstance(item, dict):
                    stripped[key] = {
                        name: without_descriptions(child)
                        for name, child in item.items()
                    }
                else:
                    stripped[key] = without_descriptions(item)
            return stripped
        if isinstance(value, list):
            return [without_descriptions(item) for item in value]
        return value

    def canonical_text(value: object) -> str:
        """Serialize type-strictly so booleans never equal 0 or 1."""
        return json.dumps(value, sort_keys=True, indent=1, ensure_ascii=False)

    json_schema = json.loads(
        (SCHEMA_DIR / "canonical-event.schema.json").read_text(encoding="utf-8")
    )
    yaml_schema = yaml.safe_load(
        (SCHEMA_DIR / "canonical-event.schema.yaml").read_text(encoding="utf-8")
    )
    assert canonical_text(without_descriptions(json_schema)) == canonical_text(
        without_descriptions(yaml_schema)
    )
