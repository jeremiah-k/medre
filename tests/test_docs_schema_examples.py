"""Schema and example validation tests.

Asserts that:

  1. Each JSON example in docs/schemas/examples/ is valid JSON.
  2. Each example validates against its corresponding schema.
  3. If jsonschema is available, perform full schema validation;
     otherwise, manually check that all required fields exist with
     correct types.
  4. Schemas have not drifted from source (basic required-field
     name check against example payloads).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _ROOT / "docs" / "schemas"
_EXAMPLES_DIR = _SCHEMAS_DIR / "examples"

# ---------------------------------------------------------------------------
# Try importing jsonschema (optional dependency)
# ---------------------------------------------------------------------------

try:
    import jsonschema as _jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    """Read file contents as UTF-8 string."""
    return path.read_text(encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    """Load and parse a JSON file."""
    return json.loads(_read(path))


#: Mapping from example filename to its corresponding schema filename.
#: The naming convention is ``<stem>-example.json`` → ``<stem>.schema.json``.
def _example_schema_pairs() -> list[tuple[Path, Path]]:
    """Return (example_path, schema_path) pairs for all examples."""
    if not _EXAMPLES_DIR.is_dir():
        return []
    pairs: list[tuple[Path, Path]] = []
    for example in sorted(_EXAMPLES_DIR.glob("*.json")):
        # Extract the schema stem: "canonical-event-example" → "canonical-event"
        name = example.stem
        if name.endswith("-example"):
            schema_stem = name[: -len("-example")]
        else:
            schema_stem = name
        schema = _SCHEMAS_DIR / f"{schema_stem}.schema.json"
        if schema.exists():
            pairs.append((example, schema))
    return pairs


def _get_required_fields(schema: dict[str, Any]) -> list[str]:
    """Extract required field names from a JSON Schema object."""
    return schema.get("required", [])


def _get_property_types(schema: dict[str, Any]) -> dict[str, Any]:
    """Extract property type declarations from a JSON Schema."""
    props = schema.get("properties", {})
    return {name: prop.get("type") for name, prop in props.items()}


def _check_type(value: Any, type_decl: Any) -> bool:
    """Check if a value matches a JSON Schema type declaration.

    Handles single types (``"string"``), union types
    (``["string", "null"]``), and None declarations (returns True).
    """
    if type_decl is None:
        return True
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    if isinstance(type_decl, list):
        return any(_check_type(value, t) for t in type_decl)
    expected = type_map.get(type_decl)
    if expected is None:
        return True
    return isinstance(value, expected)


# ===========================================================================
# 1. Examples are valid JSON
# ===========================================================================


class TestExamplesAreValidJson:
    """Each example file must parse as valid JSON."""

    @pytest.mark.parametrize(
        "example_path",
        sorted(_EXAMPLES_DIR.glob("*.json")) if _EXAMPLES_DIR.is_dir() else [],
        ids=lambda p: p.name,
    )
    def test_example_is_valid_json(self, example_path: Path) -> None:
        """Example file must parse without error."""
        try:
            _load_json(example_path)
        except json.JSONDecodeError as exc:
            pytest.fail(f"{example_path.name}: invalid JSON: {exc}")


# ===========================================================================
# 2. Examples validate against schemas
# ===========================================================================


class TestExamplesValidateAgainstSchemas:
    """Each example must validate against its corresponding schema."""

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_example_validates_against_schema(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Example must satisfy all required fields from its schema."""
        example = _load_json(example_path)
        schema = _load_json(schema_path)

        if _HAS_JSONSCHEMA:
            import jsonschema

            jsonschema.validate(instance=example, schema=schema)
        else:
            # Manual validation: check required fields exist
            required = _get_required_fields(schema)
            for field in required:
                assert field in example, (
                    f"{example_path.name}: missing required field '{field}' "
                    f"from {schema_path.name}"
                )

            # Check types for present fields
            prop_types = _get_property_types(schema)
            for field, type_decl in prop_types.items():
                if field in example and type_decl is not None:
                    assert _check_type(example[field], type_decl), (
                        f"{example_path.name}: field '{field}' has type "
                        f"{type(example[field]).__name__}, expected {type_decl}"
                    )

    def test_schema_exists_for_each_example(self) -> None:
        """Every example file must have a corresponding schema file."""
        if not _EXAMPLES_DIR.is_dir():
            pytest.skip("docs/schemas/examples/ not found")
        examples = sorted(_EXAMPLES_DIR.glob("*.json"))
        for example in examples:
            name = example.stem
            schema_stem = (
                name[: -len("-example")] if name.endswith("-example") else name
            )
            schema = _SCHEMAS_DIR / f"{schema_stem}.schema.json"
            assert schema.exists(), (
                f"No schema found for example {example.name}. "
                f"Expected: {schema.relative_to(_ROOT)}"
            )


# ===========================================================================
# 3. Schema required fields present in examples (drift detection)
# ===========================================================================


class TestSchemaExampleFieldDrift:
    """Detect drift between schemas and examples.

    If a schema declares a required field that is absent from the example,
    or the example has a top-level field not declared in the schema, the
    test fails — indicating that schema and example have diverged.
    """

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_all_required_fields_present_in_example(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Every required field from the schema must appear in the example."""
        example = _load_json(example_path)
        schema = _load_json(schema_path)
        required = _get_required_fields(schema)
        missing = [f for f in required if f not in example]
        assert not missing, (
            f"{example_path.name}: missing required fields from "
            f"{schema_path.name}: {missing}"
        )

    @pytest.mark.parametrize(
        "example_path,schema_path",
        _example_schema_pairs(),
        ids=lambda p: p.name if isinstance(p, Path) else str(p),
    )
    def test_example_fields_match_schema(
        self, example_path: Path, schema_path: Path
    ) -> None:
        """Top-level example fields must be declared in the schema.

        This catches schema drift where examples have fields that the
        schema doesn't document.  Allows ``additionalProperties: true``
        schemas to pass — only flags when the schema explicitly sets
        ``additionalProperties: false``.
        """
        example = _load_json(example_path)
        schema = _load_json(schema_path)

        # Only check if schema disallows additional properties.
        if schema.get("additionalProperties", True) is not False:
            return

        schema_props = set(schema.get("properties", {}).keys())
        example_fields = set(example.keys())
        extra = example_fields - schema_props
        assert not extra, (
            f"{example_path.name}: has top-level fields not in "
            f"{schema_path.name}: {sorted(extra)}. "
            f"Either update the schema or remove the extra fields."
        )
