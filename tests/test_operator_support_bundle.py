"""Tests for ``medre.runtime.support_bundle.create_support_bundle``.

Covers the bundle model: ZIP structure, per-member shape, valid and
invalid config handling, default vs. explicit output path.

The bundle is observational-only (no adapter I/O); these tests exercise
the writer directly, not the CLI. CLI-level tests live in
``test_cli_support_bundle.py``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from medre.runtime.support_bundle import (
    BUNDLE_SCHEMA_VERSION,
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
      access_token: tok
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
    # The access_token value is redacted (token does not appear).
    assert "tok" not in text


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
