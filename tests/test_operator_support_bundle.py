"""Tests for ``medre.runtime.support_bundle.create_support_bundle``.

Covers the bundle model: ZIP structure, per-member shape, valid and
invalid config handling, default vs. explicit output path.

The bundle is observational-only (no adapter I/O); these tests exercise
the writer directly, not the CLI. CLI-level tests live in
``test_cli_support_bundle.py``.
"""

from __future__ import annotations

import dataclasses
import json
import zipfile
from pathlib import Path
from typing import Any

import msgspec
import pytest

from medre.runtime.support_bundle import (
    BUNDLE_SCHEMA_VERSION,
    ConfigCheckMember,
    ConfigSourceMember,
    EnvironmentMember,
    ManifestMember,
    SchemaEntry,
    SchemasMember,
    _has_config_env_overrides,
    _json_bytes,
    _json_default,
    _read_schema_meta,
    _to_builtins,
    create_support_bundle,
)

# ---------------------------------------------------------------------------
# Shared config constants (inline YAML strings)
# ---------------------------------------------------------------------------

# Valid fake-adapter bridge: Matrix <-> Meshtastic with one route.
CONFIG_VALID = """\
runtime:
  name: support-bundle-test
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: s3cret-bundle-test-token
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: TestMesh
routes:
  matrix_to_radio:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    enabled: true
"""

# Adapters but no routes section.
CONFIG_NO_ROUTES = """\
runtime:
  name: no-routes-test
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# Valid YAML syntax but fails runtime validation (negative limit).
CONFIG_BAD_LIMITS = """\
runtime:
  name: bad-limits-test
  limits:
    max_inflight_deliveries: -1
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: tok
      room_allowlist: ['!room:test']
      encryption_mode: plaintext
"""

# Matrix + MeshCore BLE (with ble_pin) for verifying secret-presence and
# raw-value redaction across transports. No SDK is imported: only the
# frozen config dataclasses are constructed by the loader.
CONFIG_WITH_MESHCORE = """\
runtime:
  name: meshcore-secret-test
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://matrix.test
      user_id: '@bot:test'
      access_token: s3cret-bundle-test-token
      encryption_mode: plaintext
  meshcore:
    node:
      enabled: true
      adapter_kind: fake
      connection_type: ble
      ble_address: 'AA:BB:CC:DD:EE:FF'
      ble_pin: 'pin-raw-value-4321'
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent test-runner MEDRE_HOME/MEDRE_CONFIG from interfering."""
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


def _write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    """Write *text* to a temp YAML file and return its path."""
    p = tmp_path / name
    p.write_text(text)
    return p


def _read_bundle(zip_path: Path) -> dict[str, bytes]:
    """Return a dict of {member_name: raw_bytes} from a support bundle ZIP."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _read_json_member(members: dict[str, bytes], name: str) -> dict[str, Any]:
    """Parse a JSON object member from the bundle dict."""
    return json.loads(members[name].decode("utf-8"))


# ---------------------------------------------------------------------------
# Bundle model: ZIP existence and required members
# ---------------------------------------------------------------------------


def test_valid_config_produces_zip(tmp_path: Path) -> None:
    """A valid config produces a ZIP file that exists and is a valid archive."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    out = tmp_path / "bundle.zip"
    result = create_support_bundle(config_path=cfg, output_path=out)
    assert out.is_file()
    assert result == out.resolve()
    with zipfile.ZipFile(out, "r") as zf:
        assert zf.testzip() is None  # no corrupt members


def test_zip_contains_manifest_json(tmp_path: Path) -> None:
    """manifest.json has bundle_schema_version, created_at, and platform info."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    manifest = _read_json_member(members, "manifest.json")
    assert manifest["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION
    assert isinstance(manifest["created_at"], str)
    assert manifest["created_at"] != ""
    assert "platform" in manifest
    assert "python_version" in manifest["platform"]
    assert manifest["command"] == "medre support bundle"


def test_zip_contains_environment_json(tmp_path: Path) -> None:
    """environment.json has python_version, platform, and medre_version."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    env = _read_json_member(members, "environment.json")
    assert "python_version" in env
    assert "platform" in env
    assert "medre_version" in env


def test_zip_contains_config_check_valid(tmp_path: Path) -> None:
    """config_check.json reports success=true for a valid config."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    check = _read_json_member(members, "config_check.json")
    assert check["success"] is True
    assert check["error"] is None


def test_zip_contains_route_plan(tmp_path: Path) -> None:
    """route_plan.json has routes, adapters, legs, and total_legs for valid config."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    plan = _read_json_member(members, "route_plan.json")
    assert "routes" in plan
    assert "adapters" in plan
    assert "total_legs" in plan
    assert isinstance(plan["routes"], list)
    assert len(plan["routes"]) >= 1
    # At least one route should have a non-empty legs list.
    has_legs = any(len(route.get("legs", [])) > 0 for route in plan["routes"])
    assert has_legs, f"expected at least one route with legs, got: {plan['routes']}"


def test_zip_contains_adapters_json(tmp_path: Path) -> None:
    """adapters.json lists adapter IDs, transports, and enabled status."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    adapters_doc = _read_json_member(members, "adapters.json")
    adapters = adapters_doc["adapters"]
    assert len(adapters) == 2
    by_id = {a["adapter_id"]: a for a in adapters}
    assert "main" in by_id
    assert "radio" in by_id
    assert by_id["main"]["transport"] == "matrix"
    assert by_id["radio"]["transport"] == "meshtastic"
    assert by_id["main"]["enabled"] is True
    assert by_id["radio"]["enabled"] is True


def test_zip_contains_redacted_config_yaml(tmp_path: Path) -> None:
    """redacted_config.yaml is present and contains redacted config text."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    assert "redacted_config.yaml" in members
    text = members["redacted_config.yaml"].decode("utf-8")
    # Non-secret structural keys are preserved.
    assert "adapters" in text
    assert "matrix" in text
    # The access_token value is redacted (the unique secret does not appear).
    # The key name survives redaction by design; the assertion uses a value
    # that is not a substring of any key name so it only fails on a real leak.
    assert "s3cret-bundle-test-token" not in text


# ---------------------------------------------------------------------------
# Invalid config handling
# ---------------------------------------------------------------------------


def test_invalid_config_still_produces_zip(tmp_path: Path) -> None:
    """An invalid config still produces a ZIP; config_check reports failure."""
    cfg = _write_config(tmp_path, CONFIG_BAD_LIMITS)
    out = tmp_path / "bundle.zip"
    create_support_bundle(config_path=cfg, output_path=out)
    assert out.is_file()
    members = _read_bundle(out)
    check = _read_json_member(members, "config_check.json")
    assert check["success"] is False
    assert isinstance(check["error"], str)
    assert check["error"] != ""


def test_invalid_config_route_plan_has_error(tmp_path: Path) -> None:
    """route_plan.json has an 'error' key when config fails to load."""
    cfg = _write_config(tmp_path, CONFIG_BAD_LIMITS)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    plan = _read_json_member(members, "route_plan.json")
    assert "error" in plan
    assert isinstance(plan["error"], str)
    assert plan["error"] != ""


def test_invalid_config_adapters_empty(tmp_path: Path) -> None:
    """adapters.json has an empty list when config fails to load."""
    cfg = _write_config(tmp_path, CONFIG_BAD_LIMITS)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    adapters_doc = _read_json_member(members, "adapters.json")
    assert adapters_doc["adapters"] == []


# ---------------------------------------------------------------------------
# Edge cases: no routes, output path
# ---------------------------------------------------------------------------


def test_no_routes_config_has_empty_routes(tmp_path: Path) -> None:
    """A config with no routes section produces an empty routes list in the plan."""
    cfg = _write_config(tmp_path, CONFIG_NO_ROUTES)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    plan = _read_json_member(members, "route_plan.json")
    assert plan["routes"] == []
    assert plan["total_legs"] == 0
    # Adapters are still listed even with no routes.
    assert len(plan["adapters"]) == 1
    assert plan["adapters"][0]["adapter_id"] == "main"


def test_output_path_respected(tmp_path: Path) -> None:
    """The ZIP is written to the explicitly specified output path."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    custom = tmp_path / "subdir" / "custom-bundle.zip"
    custom.parent.mkdir()
    result = create_support_bundle(config_path=cfg, output_path=custom)
    assert custom.is_file()
    assert result == custom.resolve()


def test_default_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no output_path, defaults to medre-support-bundle.zip in CWD."""
    monkeypatch.chdir(tmp_path)
    # No config either: bundle still writes with config_check failure.
    create_support_bundle()
    default = tmp_path / "medre-support-bundle.zip"
    assert default.is_file()
    members = _read_bundle(default)
    # Manifest is always present regardless of config status.
    assert "manifest.json" in members


def test_manifest_always_present_regardless_of_config(tmp_path: Path) -> None:
    """manifest.json and environment.json are present even when config is missing."""
    # Point at a non-existent config file; find_config raises, bundle continues.
    out = tmp_path / "b.zip"
    create_support_bundle(config_path=tmp_path / "nonexistent.yaml", output_path=out)
    members = _read_bundle(out)
    assert "manifest.json" in members
    assert "environment.json" in members
    assert "config_check.json" in members
    check = _read_json_member(members, "config_check.json")
    assert check["success"] is False


def test_config_source_member_records_explicit_path(tmp_path: Path) -> None:
    """config_source.json records the discovered source and path."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    src = _read_json_member(members, "config_source.json")
    assert src["source"] == "explicit"
    assert src["path"] is not None
    assert str(cfg) in src["path"] or cfg.name in src["path"]


# ---------------------------------------------------------------------------
# Env-override detection (config_source.json::env_overrides_applied)
# ---------------------------------------------------------------------------


def test_env_override_adapter_prefix_detected() -> None:
    """MEDRE_ADAPTER__* sets env_overrides_applied True."""
    env = {"MEDRE_ADAPTER__MATRIX__HOMESERVER": "https://x.test"}
    assert _has_config_env_overrides(env) is True


def test_env_override_route_prefix_detected() -> None:
    """MEDRE_ROUTE__* sets env_overrides_applied True."""
    env = {"MEDRE_ROUTE__R1__ENABLED": "true"}
    assert _has_config_env_overrides(env) is True


def test_env_override_retry_prefix_detected() -> None:
    """MEDRE_RETRY__* sets env_overrides_applied True."""
    env = {"MEDRE_RETRY__MAX_ATTEMPTS": "5"}
    assert _has_config_env_overrides(env) is True


def test_env_override_db_path_exact_detected() -> None:
    """MEDRE_DB_PATH sets env_overrides_applied True."""
    assert _has_config_env_overrides({"MEDRE_DB_PATH": "/tmp/x.db"}) is True


def test_env_override_log_level_exact_detected() -> None:
    """MEDRE_LOG_LEVEL sets env_overrides_applied True."""
    assert _has_config_env_overrides({"MEDRE_LOG_LEVEL": "DEBUG"}) is True


def test_env_override_runtime_limit_exact_detected() -> None:
    """MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES sets env_overrides_applied True."""
    env = {"MEDRE_RUNTIME_MAX_INFLIGHT_DELIVERIES": "10"}
    assert _has_config_env_overrides(env) is True


def test_env_override_config_discovery_not_an_override() -> None:
    """MEDRE_CONFIG is discovery, not an override — returns False."""
    assert _has_config_env_overrides({"MEDRE_CONFIG": "/tmp/c.yaml"}) is False


def test_env_override_home_discovery_not_an_override() -> None:
    """MEDRE_HOME is discovery, not an override — returns False."""
    assert _has_config_env_overrides({"MEDRE_HOME": "/tmp/medre"}) is False


def test_env_override_unknown_medre_var_not_an_override() -> None:
    """Unknown MEDRE_* vars are not overrides — returns False."""
    assert _has_config_env_overrides({"MEDRE_FUTURE_FEATURE": "1"}) is False


def test_env_override_empty_environ() -> None:
    """An empty environ yields False."""
    assert _has_config_env_overrides({}) is False


def test_config_source_no_raw_env_values_in_bundle(tmp_path: Path) -> None:
    """config_source.json never carries env-var values, only the boolean flag."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    src_text = members["config_source.json"].decode("utf-8")
    assert "env_overrides_applied" in src_text
    # The flag is a JSON boolean, not a name/value list.
    assert "MEDRE_ADAPTER__" not in src_text
    assert "MEDRE_HOME" not in src_text


# ---------------------------------------------------------------------------
# schemas.json member
# ---------------------------------------------------------------------------


def test_schemas_json_present_in_bundle(tmp_path: Path) -> None:
    """schemas.json is a bundle member."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    assert "schemas.json" in members


def test_schemas_json_reports_runtime_schema_presence(tmp_path: Path) -> None:
    """schemas.json records whether the runtime config schema is present."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    schemas = _read_json_member(members, "schemas.json")
    rt = schemas["runtime_config_schema"]
    assert rt["present"] is True
    assert isinstance(rt["path"], str)
    assert rt["path"] != ""


def test_schemas_json_reports_runtime_schema_id(tmp_path: Path) -> None:
    """schemas.json surfaces the runtime schema $id when the schema is present."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    schemas = _read_json_member(members, "schemas.json")
    rt = schemas["runtime_config_schema"]
    assert rt["$id"] is not None
    assert isinstance(rt["$id"], str)
    assert "runtime-config" in rt["$id"]


def test_schemas_json_reports_validate_script_presence(tmp_path: Path) -> None:
    """schemas.json records whether validate-example-configs.sh exists."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    schemas = _read_json_member(members, "schemas.json")
    assert "validate_example_configs_script_present" in schemas
    assert isinstance(schemas["validate_example_configs_script_present"], bool)


def test_schemas_json_has_no_secret_values(tmp_path: Path) -> None:
    """schemas.json contains schema metadata only — no secret substrings."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    text = members["schemas.json"].decode("utf-8")
    assert "s3cret-bundle-test-token" not in text
    assert "access_token" not in text


def test_schemas_json_present_even_when_config_missing(tmp_path: Path) -> None:
    """schemas.json is always present, even when config discovery fails."""
    out = tmp_path / "b.zip"
    create_support_bundle(config_path=tmp_path / "nonexistent.yaml", output_path=out)
    members = _read_bundle(out)
    assert "schemas.json" in members
    schemas = _read_json_member(members, "schemas.json")
    # Each schema entry has a `present` key regardless of repo layout.
    assert "runtime_config_schema" in schemas
    assert "present" in schemas["runtime_config_schema"]


# ---------------------------------------------------------------------------
# schemas.json::evidence_bundle_schema (added alongside runtime/adapter/
# routing schemas so support can spot evidence-schema drift too)
# ---------------------------------------------------------------------------


def test_schemas_json_reports_evidence_bundle_schema_presence(tmp_path: Path) -> None:
    """schemas.json records evidence-bundle schema presence."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    schemas = _read_json_member(members, "schemas.json")
    eb = schemas["evidence_bundle_schema"]
    assert eb["present"] is True
    assert isinstance(eb["path"], str)
    assert eb["path"].endswith("evidence-bundle.schema.json")


def test_schemas_json_reports_evidence_bundle_schema_id(tmp_path: Path) -> None:
    """schemas.json surfaces the evidence-bundle schema $id when present."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    schemas = _read_json_member(members, "schemas.json")
    eb = schemas["evidence_bundle_schema"]
    assert isinstance(eb["$id"], str)
    assert "evidence-bundle" in eb["$id"]


# ---------------------------------------------------------------------------
# adapters.json enrichment: adapter_kind, connection_type,
# endpoint_fields_present, secret_fields_present (and raw-value absence)
# ---------------------------------------------------------------------------


def test_adapters_json_includes_adapter_kind(tmp_path: Path) -> None:
    """adapters.json reports adapter_kind ('fake' for the test configs)."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    by_id = {
        a["adapter_id"]: a
        for a in _read_json_member(members, "adapters.json")["adapters"]
    }
    assert by_id["main"]["adapter_kind"] == "fake"
    assert by_id["radio"]["adapter_kind"] == "fake"


def test_adapters_json_includes_connection_type_for_meshtastic(tmp_path: Path) -> None:
    """adapters.json reports connection_type for transports that have one."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    by_id = {
        a["adapter_id"]: a
        for a in _read_json_member(members, "adapters.json")["adapters"]
    }
    # Meshtastic radio exposes connection_type=fake.
    assert by_id["radio"]["connection_type"] == "fake"
    # Matrix has no connection_type attribute — the field is omitted, not null.
    assert "connection_type" not in by_id["main"]


def test_adapters_json_includes_endpoint_fields_present_for_matrix(
    tmp_path: Path,
) -> None:
    """adapters.json endpoint_fields_present reports homeserver/user_id/room_allowlist for Matrix."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    by_id = {
        a["adapter_id"]: a
        for a in _read_json_member(members, "adapters.json")["adapters"]
    }
    efp = by_id["main"]["endpoint_fields_present"]
    assert efp["homeserver"] is True
    assert efp["user_id"] is True
    assert efp["room_allowlist"] is True
    # No host field on matrix — must not appear.
    assert "host" not in efp


def test_adapters_json_includes_secret_fields_present_for_matrix(
    tmp_path: Path,
) -> None:
    """adapters.json secret_fields_present reports access_token=true for Matrix."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    by_id = {
        a["adapter_id"]: a
        for a in _read_json_member(members, "adapters.json")["adapters"]
    }
    sfp = by_id["main"]["secret_fields_present"]
    assert sfp == {"access_token": True}


def test_adapters_json_does_not_include_raw_access_token(tmp_path: Path) -> None:
    """adapters.json never carries the raw access_token value — only boolean presence."""
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    text = members["adapters.json"].decode("utf-8")
    # The unique token value is not a substring of any key name, so this
    # assertion only fails on a real leak.
    assert "s3cret-bundle-test-token" not in text
    # The secret field name is allowed (it is the key in the presence dict);
    # only its value must be absent. The presence dict serialises as
    # {"access_token": true} — assert that exact shape.
    adapters = _read_json_member(members, "adapters.json")["adapters"]
    matrix = next(a for a in adapters if a["transport"] == "matrix")
    assert matrix["secret_fields_present"]["access_token"] is True


def test_adapters_json_does_not_include_raw_ble_pin(tmp_path: Path) -> None:
    """adapters.json never carries the raw MeshCore ble_pin value."""
    cfg = _write_config(tmp_path, CONFIG_WITH_MESHCORE)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))
    text = members["adapters.json"].decode("utf-8")
    assert "pin-raw-value-4321" not in text
    # And the presence flag is reported true without the value.
    meshcore = next(
        a
        for a in _read_json_member(members, "adapters.json")["adapters"]
        if a["transport"] == "meshcore"
    )
    assert meshcore["secret_fields_present"] == {"ble_pin": True}
    # Endpoint-ish fields for the BLE-configured MeshCore node are reported.
    assert meshcore["endpoint_fields_present"]["ble_address"] is True
    assert meshcore["connection_type"] == "ble"


# ---------------------------------------------------------------------------
# Typed bundle members are msgspec.Struct subclasses
# ---------------------------------------------------------------------------


def test_bundle_member_models_are_msgspec_structs() -> None:
    """Bundle member models are msgspec.Struct subclasses.

    Regression guard: ensures the typed models are not accidentally
    swapped back to plain dataclasses or removed.
    """
    for model in (
        ManifestMember,
        EnvironmentMember,
        ConfigSourceMember,
        ConfigCheckMember,
        SchemaEntry,
        SchemasMember,
    ):
        assert issubclass(model, msgspec.Struct), model


def test_manifest_member_is_frozen() -> None:
    """ManifestMember is frozen (immutable snapshot)."""
    m = ManifestMember(
        bundle_schema_version=1,
        created_at="2024-01-01T00:00:00+00:00",
        command="medre support bundle",
        medre_version="0.1.0",
        platform={"python_version": "3.11.0", "platform": "linux", "machine": "x86_64"},
        redaction_policy="secret-key-name-match-v1",
    )
    with pytest.raises(AttributeError):
        m.command = "other"  # type: ignore[misc]


def test_config_check_member_is_mutable() -> None:
    """ConfigCheckMember allows incremental field mutation."""
    c = ConfigCheckMember()
    assert c.success is False
    assert c.error is None
    c.success = True
    c.error = "boom"
    assert c.success is True
    assert c.error == "boom"


def test_schema_entry_uses_dollar_prefixed_json_keys() -> None:
    """SchemaEntry serialises with $id / $schema JSON keys (not Python names)."""
    entry = SchemaEntry(present=True, path="/x", id="https://id", schema="https://sch")
    builtins = msgspec.to_builtins(entry)
    assert "$id" in builtins
    assert "$schema" in builtins
    assert "id" not in builtins
    assert "schema" not in builtins
    assert builtins["$id"] == "https://id"
    assert builtins["$schema"] == "https://sch"


# ---------------------------------------------------------------------------
# _to_builtins / _json_bytes: recursive serialisation of mixed
# msgspec.Struct + dataclass payloads at arbitrary depth.
# ---------------------------------------------------------------------------
#
# Regression guard for two gaps that previously let raw Struct instances
# reach json.dumps (whose ``_json_default`` only handles dataclasses,
# not Structs):
#
#   1. tuples were not traversed — a Struct inside a tuple survived
#      untouched and raised TypeError at json.dumps time.
#   2. dataclass conversion returned ``dataclasses.asdict(obj)`` directly
#      without recursing the result, so a Struct-valued field survived
#      asdict as a raw Struct and raised TypeError at json.dumps time.


@dataclasses.dataclass
class _NestedDataclass:
    """Plain dataclass used to exercise asdict-then-recurse behaviour."""

    label: str
    count: int


def test_to_builtins_converts_tuple_of_struct_to_list() -> None:
    """A tuple containing a Struct is converted to a list of plain dicts.

    Previously tuples fell through ``_to_builtins`` unchanged, so a
    Struct inside a tuple reached json.dumps and raised TypeError.
    """
    env = EnvironmentMember(
        python_version="3.11",
        platform="linux",
        machine="x86_64",
        medre_version="0",
    )
    result = _to_builtins((env, env))
    assert isinstance(result, list)
    assert result == [
        {
            "python_version": "3.11",
            "platform": "linux",
            "machine": "x86_64",
            "medre_version": "0",
        },
        {
            "python_version": "3.11",
            "platform": "linux",
            "machine": "x86_64",
            "medre_version": "0",
        },
    ]


def test_to_builtins_recurses_struct_nested_in_dataclass_field() -> None:
    """A Struct nested inside a dataclass field is converted to a dict.

    Previously ``dataclasses.asdict`` was returned directly without
    recursion; the Struct-valued field survived as a raw Struct and
    raised TypeError at json.dumps time.
    """

    @dataclasses.dataclass
    class Holder:
        label: str
        env: EnvironmentMember

    holder = Holder(
        label="x",
        env=EnvironmentMember(
            python_version="3.11",
            platform="linux",
            machine="x86_64",
            medre_version="0",
        ),
    )
    result = _to_builtins(holder)
    assert isinstance(result, dict)
    assert isinstance(result["env"], dict), result
    assert result["env"]["python_version"] == "3.11"
    assert not isinstance(result["env"], msgspec.Struct)


def test_json_bytes_handles_mixed_struct_and_dataclass_payload() -> None:
    """``_json_bytes`` serialises a payload that mixes Struct and dataclass
    instances nested inside dicts, lists, and tuples at multiple depths.

    Asserts:
      - the result parses as JSON (no TypeError leaked),
      - no Struct or dataclass instance survives into the parsed output,
      - SchemaEntry ``$id`` / ``$schema`` aliases are preserved end-to-end.
    """

    @dataclasses.dataclass
    class Holder:
        label: str
        entry: SchemaEntry
        envs: tuple

    payload = {
        "name": "mixed",
        "holder": Holder(
            label="h",
            entry=SchemaEntry(
                present=True, path="/p", id="https://id", schema="https://sch"
            ),
            envs=(
                EnvironmentMember(
                    python_version="3.11",
                    platform="linux",
                    machine="x86_64",
                    medre_version="0",
                ),
            ),
        ),
        "items": [
            _NestedDataclass(label="a", count=1),
            {
                "nested_struct": EnvironmentMember(
                    python_version="3.12",
                    platform="darwin",
                    machine="arm64",
                    medre_version="1",
                )
            },
        ],
        "top_struct": SchemasMember(
            runtime_config_schema=SchemaEntry(present=True, path="/r"),
            adapter_config_schema=SchemaEntry(present=True, path="/a"),
            routing_config_schema=SchemaEntry(present=True, path="/rt"),
            evidence_bundle_schema=SchemaEntry(present=True, path="/e"),
            validate_example_configs_script_present=True,
        ),
    }

    encoded = _json_bytes(payload)
    decoded = json.loads(encoded.decode("utf-8"))

    # No Struct or dataclass leaks into the JSON output.
    _assert_no_struct_or_dataclass(decoded)

    # SchemaEntry aliases preserved end-to-end through _json_bytes.
    holder_entry = decoded["holder"]["entry"]
    assert "$id" in holder_entry
    assert "$schema" in holder_entry
    assert "id" not in holder_entry
    assert "schema" not in holder_entry
    assert holder_entry["$id"] == "https://id"
    assert holder_entry["$schema"] == "https://sch"

    # Nested dataclass fields converted to plain dicts.
    assert decoded["items"][0] == {"label": "a", "count": 1}
    # Nested Struct inside a list-of-dict converted to plain dict.
    assert decoded["items"][1]["nested_struct"]["python_version"] == "3.12"
    # Tuple of Structs serialised as a JSON list of dicts.
    assert isinstance(decoded["holder"]["envs"], list)
    assert decoded["holder"]["envs"][0]["platform"] == "linux"


def _assert_no_struct_or_dataclass(value: Any) -> None:
    """Recursively assert *value* holds no msgspec.Struct or dataclass."""
    if isinstance(value, msgspec.Struct):
        raise AssertionError(f"struct leaked into JSON output: {value!r}")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        raise AssertionError(f"dataclass leaked into JSON output: {value!r}")
    if isinstance(value, dict):
        for v in value.values():
            _assert_no_struct_or_dataclass(v)
    elif isinstance(value, list):
        for v in value:
            _assert_no_struct_or_dataclass(v)


# ---------------------------------------------------------------------------
# Cross-type regression: Struct with a nested dataclass field, and
# _json_default fallback for a dataclass with a nested Struct field.
# ---------------------------------------------------------------------------
#
# msgspec.Struct fields accept any Python type at runtime; modern msgspec
# converts nested dataclass values via msgspec.to_builtins, but that is
# an implementation detail of msgspec, not a contract this module relies
# on. The first test below locks in _to_builtins' end-to-end behaviour
# for a Struct-with-dataclass-field shape so a future msgspec change or
# a new field shape (e.g. a tuple of dataclasses inside a Struct) cannot
# silently regress to leaving raw dataclass instances in the output.
#
# The second test exercises _json_default directly. _json_default is a
# defensive fallback that only fires when _to_builtins missed a value;
# if it ever does, the dataclass branch must produce a fully normalised
# dict (no raw Struct surviving in a field value).


def test_to_builtins_normalises_struct_with_nested_dataclass_field() -> None:
    """A Struct with a dataclass-valued field serialises to plain dicts.

    Covers the recursion added to the Struct branch of _to_builtins:
    msgspec.to_builtins is called first, then its output is fed back
    through _to_builtins so any container shape (tuples, nested
    dataclasses) is normalised to this function's contract.

    The Struct also carries a tuple-of-dataclasses field and a
    dict-of-dataclasses field. msgspec.to_builtins leaves tuples as
    tuples; the recursion through _to_builtins converts them to lists,
    matching the rest of the serialisation contract.
    """

    @dataclasses.dataclass
    class Inner:
        count: int

    class Outer(msgspec.Struct):
        label: str
        inner: Inner
        items: tuple
        mapping: dict

    obj = Outer(
        label="x",
        inner=Inner(count=5),
        items=(Inner(count=1), Inner(count=2)),
        mapping={"k": Inner(count=3)},
    )
    result = _to_builtins(obj)

    assert result == {
        "label": "x",
        "inner": {"count": 5},
        "items": [{"count": 1}, {"count": 2}],
        "mapping": {"k": {"count": 3}},
    }, result
    # Tuples must be normalised to lists (msgspec.to_builtins alone
    # leaves them as tuples, which is the gap the recursion closes).
    assert isinstance(result["items"], list)
    # No dataclass or Struct instance survives into the output.
    _assert_no_struct_or_dataclass(result)


def test_json_default_dataclass_fallback_recurses_through_struct() -> None:
    """_json_default's dataclass branch returns a fully normalised dict.

    Regression: previously the dataclass branch returned
    ``dataclasses.asdict(obj)`` directly, which left Struct-valued
    fields as raw msgspec.Struct instances. Although json.dumps would
    eventually call ``default`` again on each surviving Struct, the
    return value itself was not JSON-safe, breaking the contract that
    ``default`` return values are serialisable.

    The fix routes asdict's output through _to_builtins so the dataclass
    branch matches the Struct branch and the main _json_bytes path.
    """

    @dataclasses.dataclass
    class Holder:
        label: str
        env: EnvironmentMember
        entries: tuple

    holder = Holder(
        label="x",
        env=EnvironmentMember(
            python_version="3.11",
            platform="linux",
            machine="x86_64",
            medre_version="0",
        ),
        entries=(
            SchemaEntry(present=True, path="/p", id="https://id", schema="https://sch"),
        ),
    )

    # Direct call: _json_default must return a value that json.dumps
    # can serialise without invoking default again.
    out = _json_default(holder)
    assert out == {
        "label": "x",
        "env": {
            "python_version": "3.11",
            "platform": "linux",
            "machine": "x86_64",
            "medre_version": "0",
        },
        "entries": [
            {
                "present": True,
                "path": "/p",
                "$id": "https://id",
                "$schema": "https://sch",
            }
        ],
    }, out

    # End-to-end: json.dumps with default=_json_default must not raise
    # and must produce a structure with no Struct/dataclass instances.
    decoded = json.loads(json.dumps(holder, default=_json_default))
    assert decoded == out
    _assert_no_struct_or_dataclass(decoded)
    # SchemaEntry $id / $schema aliases survive the fallback path.
    assert decoded["entries"][0]["$id"] == "https://id"
    assert decoded["entries"][0]["$schema"] == "https://sch"


# ---------------------------------------------------------------------------
# Per-member JSON shape-identity: valid-config and missing-config cases.
# ---------------------------------------------------------------------------
#
# These tests lock the SHAPE (key presence/absence, value TYPES) of each
# always-present bundle member. They intentionally avoid asserting exact
# values where timestamps, paths, or versions vary across environments.


def test_bundle_member_shape_valid_config(tmp_path: Path) -> None:
    """Each always-present member has the expected keys and value types
    for a valid config (manifest, environment, config_source, config_check,
    schemas). Asserts shape, not exact values.
    """
    cfg = _write_config(tmp_path, CONFIG_VALID)
    members = _read_bundle(create_support_bundle(cfg, tmp_path / "b.zip"))

    # manifest.json
    manifest = _read_json_member(members, "manifest.json")
    assert set(manifest.keys()) == {
        "bundle_schema_version",
        "created_at",
        "command",
        "medre_version",
        "platform",
        "redaction_policy",
    }
    assert isinstance(manifest["bundle_schema_version"], int)
    assert isinstance(manifest["created_at"], str) and manifest["created_at"]
    assert isinstance(manifest["command"], str) and manifest["command"]
    assert isinstance(manifest["medre_version"], str) and manifest["medre_version"]
    assert isinstance(manifest["platform"], dict)
    assert isinstance(manifest["platform"]["python_version"], str)
    assert isinstance(manifest["redaction_policy"], str)

    # environment.json
    env = _read_json_member(members, "environment.json")
    assert set(env.keys()) == {"python_version", "platform", "machine", "medre_version"}
    for v in env.values():
        assert isinstance(v, str) and v

    # config_source.json
    src = _read_json_member(members, "config_source.json")
    assert set(src.keys()) == {"source", "path", "env_overrides_applied"}
    assert isinstance(src["source"], str)
    assert isinstance(src["path"], str) and src["path"]
    assert isinstance(src["env_overrides_applied"], bool)

    # config_check.json (valid-config success path)
    check = _read_json_member(members, "config_check.json")
    assert set(check.keys()) == {"success", "error", "error_section_path"}
    assert check["success"] is True
    assert check["error"] is None
    assert check["error_section_path"] is None

    # schemas.json (all four schema entries + validator flag)
    schemas = _read_json_member(members, "schemas.json")
    assert set(schemas.keys()) == {
        "runtime_config_schema",
        "adapter_config_schema",
        "routing_config_schema",
        "evidence_bundle_schema",
        "validate_example_configs_script_present",
    }
    assert isinstance(schemas["validate_example_configs_script_present"], bool)
    for key in (
        "runtime_config_schema",
        "adapter_config_schema",
        "routing_config_schema",
        "evidence_bundle_schema",
    ):
        entry = schemas[key]
        assert set(entry.keys()) == {"present", "path", "$id", "$schema"}, key
        assert isinstance(entry["present"], bool)
        # In this repo the schemas exist; present is True with non-empty path.
        assert entry["present"] is True, key
        assert isinstance(entry["path"], str) and entry["path"], key


def test_bundle_member_shape_missing_config(tmp_path: Path) -> None:
    """The always-present members keep their shape when config discovery
    fails. config_check reports failure; config_source carries nulls.
    """
    out = tmp_path / "b.zip"
    create_support_bundle(config_path=tmp_path / "nonexistent.yaml", output_path=out)
    members = _read_bundle(out)

    # manifest.json keeps its full shape regardless of config status.
    manifest = _read_json_member(members, "manifest.json")
    assert set(manifest.keys()) == {
        "bundle_schema_version",
        "created_at",
        "command",
        "medre_version",
        "platform",
        "redaction_policy",
    }
    assert isinstance(manifest["bundle_schema_version"], int)

    # environment.json keeps its full shape.
    env = _read_json_member(members, "environment.json")
    assert set(env.keys()) == {"python_version", "platform", "machine", "medre_version"}

    # config_source.json — discovery failed: source/path are null.
    src = _read_json_member(members, "config_source.json")
    assert set(src.keys()) == {"source", "path", "env_overrides_applied"}
    assert src["source"] is None
    assert src["path"] is None
    assert isinstance(src["env_overrides_applied"], bool)

    # config_check.json — failure path with non-empty error string.
    check = _read_json_member(members, "config_check.json")
    assert set(check.keys()) == {"success", "error", "error_section_path"}
    assert check["success"] is False
    assert isinstance(check["error"], str) and check["error"]

    # schemas.json still carries its full shape — it does not depend on
    # config, so it reports real schema presence from the repo layout.
    schemas = _read_json_member(members, "schemas.json")
    assert set(schemas.keys()) == {
        "runtime_config_schema",
        "adapter_config_schema",
        "routing_config_schema",
        "evidence_bundle_schema",
        "validate_example_configs_script_present",
    }
    rt = schemas["runtime_config_schema"]
    assert set(rt.keys()) == {"present", "path", "$id", "$schema"}
    assert isinstance(rt["present"], bool)


# ---------------------------------------------------------------------------
# SchemaEntry present=false failure-shape lock.
# ---------------------------------------------------------------------------
#
# When a schema file is missing or unreadable, ``_read_schema_meta``
# returns ``SchemaEntry(present=False)``. msgspec serialises that to
# FOUR keys (the optional path/$id/$schema fields emit explicit nulls
# alongside ``present: false``). This is intentional: it gives
# operators a stable four-key shape regardless of success/failure, so
# downstream tooling does not need a key-presence branch. These tests
# lock that shape.


def test_schema_entry_failure_shape_via_read_schema_meta() -> None:
    """A missing schema file yields the locked four-key failure shape.

    Shape: ``{"present": false, "path": null, "$id": null, "$schema": null}``
    (keys are alphabetised by ``_json_bytes`` via ``sort_keys=True``).

    Intentional: ``path`` / ``$id`` / ``$schema`` appear as explicit
    ``null`` keys so consumers see a stable shape whether the schema
    was present or not. Do not "fix" the null keys away without
    coordinating the bundle schema version bump.
    """
    entry = _read_schema_meta("definitely-not-a-real-schema.schema.json")
    assert entry.present is False

    encoded = _json_bytes(entry).decode("utf-8")
    # ``_json_bytes`` uses sort_keys=True, so the alphabetical order is
    # deterministic; assert the exact serialised form to lock the shape.
    assert json.loads(encoded) == {
        "$id": None,
        "$schema": None,
        "path": None,
        "present": False,
    }


def test_schema_entry_failure_shape_in_full_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four-key failure shape survives a real ``create_support_bundle``
    run and lands in the ``schemas.json`` ZIP member.

    The repo ships real schemas, so ``_SCHEMAS_DIR`` is pointed at an
    empty path to force every lookup in ``_build_schemas_member`` to
    miss — exercising the real bundle write and the ``schemas.json``
    member end-to-end, not just the serializer.
    """
    cfg = _write_config(tmp_path, CONFIG_VALID)
    monkeypatch.setattr(
        "medre.runtime.support_bundle._SCHEMAS_DIR", tmp_path / "no-schemas"
    )

    out = create_support_bundle(config_path=cfg, output_path=tmp_path / "bundle.zip")
    members = _read_bundle(out)
    schemas = _read_json_member(members, "schemas.json")

    # Every schema entry must carry the locked four-key failure shape:
    #   {"present": false, "path": null, "$id": null, "$schema": null}
    for key in (
        "runtime_config_schema",
        "adapter_config_schema",
        "routing_config_schema",
        "evidence_bundle_schema",
    ):
        entry = schemas[key]
        assert set(entry.keys()) == {"present", "path", "$id", "$schema"}, key
        assert entry["present"] is False, key
        assert entry["path"] is None, key
        assert entry["$id"] is None, key
        assert entry["$schema"] is None, key


def test_schema_entry_present_true_contrasts_failure_shape() -> None:
    """The present=True shape emits the same four keys, with non-null
    path/$id/$schema — contrast case for the failure-shape lock above.
    """
    entry = SchemaEntry(
        present=True, path="/some/path", id="https://id", schema="https://sch"
    )
    decoded = json.loads(_json_bytes(entry).decode("utf-8"))
    assert decoded == {
        "$id": "https://id",
        "$schema": "https://sch",
        "path": "/some/path",
        "present": True,
    }
