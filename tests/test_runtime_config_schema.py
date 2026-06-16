"""Validation tests for docs/schemas/runtime-config.schema.json.

The runtime-config schema is a FULL-CONFIG schema: it validates the root
YAML document operators write and pass to ``medre run --config``. This is
distinct from the component schemas (adapter-config, routing-config) which
validate individual adapter or route objects.

These tests verify that:

  1. The schema has the required ``$id`` / ``$schema`` identifiers (also
     enforced globally by ``tests/test_schema_identifiers.py``, but checked
     here for direct attribution).
  2. The two reference full configs (``fake-bridge-smoke.yaml``,
     ``fake-multi-adapter.yaml``) validate against the schema.
  3. A config with an unknown root key is rejected — the schema enforces
     ``additionalProperties: false`` at the root, matching the loader's
     ``_KNOWN_ROOT_KEYS`` guard.
  4. The schema accepts an empty config (all sections optional).

Pattern follows ``tests/test_docs_schema_examples.py``: optional jsonschema
dependency with a manual required-field fallback when jsonschema is absent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = _ROOT / "docs" / "schemas"
_EXAMPLES_CONFIGS_DIR = _ROOT / "examples" / "configs"
_SCHEMA_PATH = _SCHEMAS_DIR / "runtime-config.schema.json"

try:
    from importlib.util import find_spec as _find_spec

    _HAS_JSONSCHEMA = _find_spec("jsonschema") is not None
    _HAS_YAML = _find_spec("yaml") is not None
except ImportError:  # pragma: no cover - importlib.util is stdlib
    _HAS_JSONSCHEMA = False
    _HAS_YAML = False


def _load_schema() -> dict[str, Any]:
    """Load and parse the runtime-config schema as a Python dict."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load and parse a YAML config file as a Python dict."""
    import yaml

    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    assert isinstance(data, dict), f"{path.name}: root YAML document must be a mapping"
    return data


# ---------------------------------------------------------------------------
# 1. Schema identifiers
# ---------------------------------------------------------------------------


class TestRuntimeConfigSchemaIdentifiers:
    """The schema must carry the standard identifiers."""

    def test_schema_file_exists(self) -> None:
        """The runtime-config schema file exists at the expected path."""
        assert _SCHEMA_PATH.is_file(), f"Missing schema: {_SCHEMA_PATH}"

    def test_schema_has_draft_2020_12(self) -> None:
        """``$schema`` must pin JSON Schema draft 2020-12."""
        schema = _load_schema()
        assert (
            schema.get("$schema")
            == "https://json-schema.org/draft/2020-12/schema"
        ), "runtime-config.schema.json: $schema must pin draft 2020-12"

    def test_schema_has_stable_id(self) -> None:
        """``$id`` must be the stable medre.dev URL with matching basename."""
        schema = _load_schema()
        id_ = schema.get("$id")
        assert isinstance(id_, str), f"$id must be a string, got {id_!r}"
        assert id_.startswith("https://medre.dev/schemas/"), (
            f"$id must be under medre.dev/schemas/, got {id_!r}"
        )
        assert id_.rsplit("/", 1)[-1] == _SCHEMA_PATH.name, (
            f"$id basename {id_!r} does not match filename {_SCHEMA_PATH.name!r}"
        )

    def test_schema_root_is_object_with_no_additional_properties(self) -> None:
        """Root schema must be an object with additionalProperties: false."""
        schema = _load_schema()
        assert schema.get("type") == "object"
        assert schema.get("additionalProperties") is False, (
            "root additionalProperties must be false (matches loader._KNOWN_ROOT_KEYS)"
        )


# ---------------------------------------------------------------------------
# 2. Reference example configs validate against the schema
# ---------------------------------------------------------------------------


_REFERENCE_CONFIGS = [
    "fake-bridge-smoke.yaml",
    "fake-multi-adapter.yaml",
]


@pytest.mark.parametrize(
    "config_name",
    _REFERENCE_CONFIGS,
    ids=_REFERENCE_CONFIGS,
)
def test_reference_config_validates_against_runtime_config_schema(
    config_name: str,
) -> None:
    """Each reference example config must validate against the full-config schema."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    if not _HAS_YAML:
        pytest.skip("yaml not installed")
    import jsonschema

    schema = _load_schema()
    config = _load_yaml(_EXAMPLES_CONFIGS_DIR / config_name)
    jsonschema.validate(instance=config, schema=schema)


# ---------------------------------------------------------------------------
# 3. Unknown root keys are rejected
# ---------------------------------------------------------------------------


def test_unknown_root_key_fails_validation() -> None:
    """An unknown root key (e.g. ``roues:``) must fail schema validation."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema

    schema = _load_schema()
    bad_config = {
        "runtime": {"name": "typo-check"},
        "roues": {},  # typo for 'routes'
    }
    with pytest.raises(jsonschema.ValidationError) as exc_info:
        jsonschema.validate(instance=bad_config, schema=schema)
    # The error should mention the offending key so operators get a hint.
    assert "roues" in exc_info.value.message, (
        f"validation error should mention 'roues', got: {exc_info.value.message!r}"
    )


def test_unknown_transport_group_fails_validation() -> None:
    """An unknown adapter transport group (e.g. ``matrixx``) must fail."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema

    schema = _load_schema()
    bad_config = {"adapters": {"matrixx": {"main": {}}}}
    with pytest.raises(jsonschema.ValidationError) as exc_info:
        jsonschema.validate(instance=bad_config, schema=schema)
    assert "matrixx" in exc_info.value.message


def test_empty_config_validates() -> None:
    """An empty config (all sections optional) must validate."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema

    schema = _load_schema()
    jsonschema.validate(instance={}, schema=schema)


# ---------------------------------------------------------------------------
# 4. Section-level additionalProperties: false
# ---------------------------------------------------------------------------


def test_unknown_runtime_section_key_fails() -> None:
    """An unknown key inside the ``runtime`` section must fail."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema

    schema = _load_schema()
    bad_config = {"runtime": {"name": "x", "bogus_key": 1}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_config, schema=schema)


def test_route_entry_with_only_required_fields_validates() -> None:
    """A minimal route entry (source_adapters + dest_adapters) validates."""
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema not installed")
    import jsonschema

    schema = _load_schema()
    config = {
        "routes": {
            "minimal": {
                "source_adapters": ["src"],
                "dest_adapters": ["dst"],
            }
        }
    }
    jsonschema.validate(instance=config, schema=schema)
