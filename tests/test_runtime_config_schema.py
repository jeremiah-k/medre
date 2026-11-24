# medre-test-tier: compatibility
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


def test_current_runtime_schema_adds_the_optional_ceiling_sweep() -> None:
    if not _HAS_JSONSCHEMA:
        pytest.skip("jsonschema is optional")
    from jsonschema import Draft202012Validator

    from medre.config.model import (
        CEILING_SWEEP_MAX_INTERVAL_SECONDS,
        CEILING_SWEEP_MAX_LIMIT,
        CeilingSweepConfig,
    )

    current = json.loads(
        (_SCHEMAS_DIR / "current" / _SCHEMA_PATH.name).read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(current)
    expected = _load_schema()
    frozen = _load_schema()
    expected["$id"] = expected["$id"].replace(
        "/schemas/runtime-config", "/schemas/current/runtime-config"
    )
    sweep = current["properties"]["retry"]["properties"]["ceiling_sweep"]
    defaults = CeilingSweepConfig()
    assert sweep["type"] == ["object", "null"]
    assert sweep["additionalProperties"] is False
    assert sweep.get("required", []) == []
    assert sweep["properties"] == {
        "enabled": {"type": "boolean", "default": defaults.enabled},
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": CEILING_SWEEP_MAX_LIMIT,
            "default": defaults.limit,
            "description": "Write an actual YAML integer such as 50, not 50.0; the runtime type validator is authoritative.",
        },
        "interval_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": CEILING_SWEEP_MAX_INTERVAL_SECONDS,
            "default": defaults.interval_seconds,
        },
    }
    # Current-schema validation tracks loader coercions; the published export
    # stays byte-for-byte unchanged. Every expected delta below is derived
    # from the frozen schema at a named leaf with a pinned literal, so an
    # unenumerated change inside any copied subtree fails the comparison.
    expected["properties"]["retry"]["properties"]["ceiling_sweep"] = sweep
    expected["properties"]["retry"]["type"] = ["object", "null"]
    expected["properties"]["retry"]["properties"]["deadline_sweep"]["type"] = [
        "object",
        "null",
    ]
    retry_numbers = expected["properties"]["retry"]["properties"]
    retry_numbers["interval_seconds"]["exclusiveMinimum"] = 0
    retry_numbers["batch_size"]["minimum"] = 1
    retry_numbers["max_attempts"]["minimum"] = 1

    frozen_logging = frozen["properties"]["logging"]["properties"]
    logging_props = expected["properties"]["logging"]["properties"]
    logging_props["level"].pop("enum")
    logging_props["level"]["pattern"] = (
        "^(?:[dD][eE][bB][uU][gG]|[iI][nN][fF][oO]|[wW][aA][rR][nN][iI][nN][gG]"
        "|[eE][rR][rR][oO][rR]|[cC][rR][iI][tT][iI][cC][aA][lL])$"
    )
    logging_props["format"].pop("enum")
    logging_props["format"]["pattern"] = "^(?:[tT][eE][xX][tT]|[jJ][sS][oO][nN])$"
    logging_props["overrides"]["propertyNames"] = {"minLength": 1}

    context_map = expected["$defs"]["RouteEntry"]["allOf"][0]["then"]["anyOf"][1][
        "properties"
    ]["context_map"]
    context_map["additionalProperties"]["type"] = "object"

    content_ceiling = expected["properties"]["content_ceiling"]
    content_ceiling["properties"]["max_age_seconds"]["type"] = ["integer", "null"]
    content_ceiling["properties"]["max_age_seconds"]["description"] = (
        "Bounded window between the trusted local admission time and the"
        " absolute UTC content ceiling (1..31536000 seconds). Null is accepted"
        " only while enabled is false; an enabled ceiling requires an integer"
        " window, and an unbounded ceiling is refused."
    )
    content_ceiling["properties"]["enabled"]["description"] = (
        "Master switch for creating hard content ceilings on new admissions."
        " While false, admission produces no content deadline; ceilings already"
        " installed on existing events remain in force and the separately"
        " enabled ceiling sweep continues to enforce them."
    )
    content_ceiling["description"] = (
        "Operator hard content privacy ceiling declaration"
        " (ContentCeilingConfig). Disabled by default: an omitted or disabled"
        " section stops installing new content ceilings at admission. Existing"
        " immutable ceilings and their read denials remain authoritative, and"
        " the independently enabled retry.ceiling_sweep may still enforce"
        " them. When enabled, the declaration states one bounded window; the"
        " producer derives each event's absolute UTC ceiling from the trusted"
        " local admission time plus that window, writes it into the immutable"
        " per-event ceiling row and the mandatory content dependency deadline"
        " inside the admission transaction, and refuses mismatched existing"
        " policies. An enabled section without max_age_seconds is refused."
    )
    accepted_window = (
        content_ceiling["then"]
        .setdefault("properties", {})
        .setdefault("max_age_seconds", {})
    )
    accepted_window["type"] = "integer"
    accepted_window["minimum"] = 1
    accepted_window["maximum"] = 31536000
    # Description-only rewordings at named leaves: the adapter and transport
    # vocabulary replaces the retired provider wording. Each delta names the
    # leaf it replaces with the pinned new literal.
    expected["description"] = (
        "Full MEDRE runtime configuration structure. Adapter transport names are"
        " extensible; installed adapter configuration models validate their"
        " fields. medre config check is the authoritative validator, including"
        " installed-adapter availability, cross-adapter identifiers, and route"
        " semantics."
    )
    expected["properties"]["adapters"]["description"] = (
        "Adapter instances grouped by transport entry-point name. The schema"
        " validates structure for any transport; medre config check rejects"
        " transports without an installed adapter and validates adapter-owned"
        " settings. An empty mapping is valid when no installed adapter"
        " contributes an offline sample; runtime startup still requires an"
        " enabled adapter."
    )
    assert current == expected

    # Independent leaf assertions: each enumerated delta names the frozen
    # leaf it replaces, so a frozen-schema change alone fails here too.
    current_logging = current["properties"]["logging"]["properties"]
    assert "enum" in frozen_logging["level"]
    assert "pattern" not in frozen_logging["level"]
    assert current_logging["level"]["pattern"] == logging_props["level"]["pattern"]
    assert "enum" in frozen_logging["format"]
    assert "pattern" not in frozen_logging["format"]
    assert current_logging["format"]["pattern"] == logging_props["format"]["pattern"]
    frozen_route = frozen["$defs"]["RouteEntry"]
    frozen_context_map = frozen_route["allOf"][0]["then"]["anyOf"][1]["properties"][
        "context_map"
    ]
    assert "type" not in frozen_context_map["additionalProperties"]
    assert (
        current["$defs"]["RouteEntry"]["allOf"][0]["then"]["anyOf"][1]["properties"][
            "context_map"
        ]["additionalProperties"]["type"]
        == "object"
    )
    assert frozen["properties"]["retry"]["properties"]["deadline_sweep"]["type"] == (
        "object"
    )
    assert current["properties"]["retry"]["properties"]["deadline_sweep"]["type"] == [
        "object",
        "null",
    ]
    assert (
        frozen["properties"]["content_ceiling"]["properties"]["max_age_seconds"]["type"]
        == "integer"
    )
    assert current["properties"]["content_ceiling"]["properties"]["max_age_seconds"][
        "type"
    ] == ["integer", "null"]
    assert "installed-provider availability" in frozen["description"]
    assert "installed-adapter availability" in current["description"]
    frozen_adapters_description = frozen["properties"]["adapters"]["description"]
    assert "provider-owned settings" in frozen_adapters_description
    assert "adapter-owned settings" in (
        current["properties"]["adapters"]["description"]
    )
    Draft202012Validator(current).validate(
        {"retry": {"ceiling_sweep": {"enabled": True}}}
    )
    Draft202012Validator(current).validate({"retry": None})


@pytest.mark.parametrize(
    "config,valid",
    [
        ({"logging": {"level": "dEbUg", "format": "JSON"}}, True),
        ({"logging": {"level": 10}}, False),
        ({"logging": {"format": "yaml"}}, False),
        (
            {"retry": {"interval_seconds": 0.5, "batch_size": 1, "max_attempts": 1}},
            True,
        ),
        ({"retry": {"interval_seconds": 0}}, False),
        ({"retry": {"batch_size": 0}}, False),
        ({"retry": {"max_attempts": 0}}, False),
        ({"content_ceiling": {"enabled": False, "max_age_seconds": None}}, True),
        ({"content_ceiling": {"enabled": True, "max_age_seconds": None}}, False),
        ({"content_ceiling": {"enabled": True, "max_age_seconds": 60}}, True),
    ],
)
def test_current_runtime_schema_checks_logging_and_retry_values(config, valid):
    validator = pytest.importorskip("jsonschema").Draft202012Validator
    current = json.loads(
        (_SCHEMAS_DIR / "current" / _SCHEMA_PATH.name).read_text(encoding="utf-8")
    )
    assert validator(current).is_valid(config) is valid


@pytest.mark.parametrize(
    "target,valid", [({"dest_context": "room"}, True), ("room", False)]
)
def test_current_runtime_schema_requires_structured_context_for_typing(target, valid):
    validator = pytest.importorskip("jsonschema").Draft202012Validator
    current = json.loads(
        (_SCHEMAS_DIR / "current" / _SCHEMA_PATH.name).read_text(encoding="utf-8")
    )
    route = {
        "source_adapters": ["source"],
        "dest_adapters": ["target"],
        "forward_typing": True,
        "context_map": {"source-channel": target},
    }
    assert validator(current).is_valid({"routes": {"bridge": route}}) is valid


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
            schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        ), "runtime-config.schema.json: $schema must pin draft 2020-12"

    def test_schema_has_stable_id(self) -> None:
        """``$id`` must be the stable repository URL with matching basename."""
        schema = _load_schema()
        id_ = schema.get("$id")
        assert isinstance(id_, str), f"$id must be a string, got {id_!r}"
        assert id_.startswith(
            "https://raw.githubusercontent.com/jeremiah-k/medre/main/docs/schemas/"
        ), f"$id must be under the repository docs/schemas URL, got {id_!r}"
        assert (
            id_.rsplit("/", 1)[-1] == _SCHEMA_PATH.name
        ), f"$id basename {id_!r} does not match filename {_SCHEMA_PATH.name!r}"

    def test_schema_root_is_object_with_no_additional_properties(self) -> None:
        """Root schema must be an object with additionalProperties: false."""
        schema = _load_schema()
        assert schema.get("type") == "object"
        assert (
            schema.get("additionalProperties") is False
        ), "root additionalProperties must be false (matches loader._KNOWN_ROOT_KEYS)"


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
    assert (
        "roues" in exc_info.value.message
    ), f"validation error should mention 'roues', got: {exc_info.value.message!r}"


def test_community_transport_group_validates_structure() -> None:
    """Provider availability is validated by the loader, not a static schema."""
    import jsonschema

    jsonschema.validate(
        {"adapters": {"community_radio": {"main": {"host": "localhost"}}}},
        _load_schema(),
    )


def test_invalid_transport_group_name_fails_validation() -> None:
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"adapters": {"Bad Transport": {}}}, _load_schema())


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
