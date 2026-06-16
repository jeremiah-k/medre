"""CLI tests for ``medre routes plan``.

Exercises the ``_routes_plan`` command through :func:`tests.helpers.cli._run_cli_raw`,
covering happy paths, error handling, and output sanitisation.  This is a
NEW file — the existing :mod:`tests.test_cli_route_commands` covers the
``validate`` / ``topology`` / ``list`` subcommands and is not extended here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.cli import _run_cli_raw

# ---------------------------------------------------------------------------
# YAML config fragments
# ---------------------------------------------------------------------------

# Two fake adapters + a simple bidirectional route.
_CONFIG_VALID = """\
runtime:
  name: plan-cli-valid
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: s3cret-token-do-not-leak
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: TestMesh
routes:
  bridge:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: bidirectional
    source_origin_label: MatrixSide
    dest_origin_label: MeshSide
  paused:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    enabled: false
"""

# Config with no routes.
_CONFIG_NO_ROUTES = """\
runtime:
  name: plan-cli-empty
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
"""

# Config referencing an unknown adapter in an enabled route.
_CONFIG_UNKNOWN_ADAPTER = """\
runtime:
  name: plan-cli-unknown
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
routes:
  broken:
    source_adapters: [nonexistent]
    dest_adapters: [main]
    directionality: source_to_dest
"""

# Matrix source + source_to_dest + duplicate rooms → ambiguous (rejected).
_CONFIG_DUPLICATE_ROOM_AMBIGUOUS = """\
runtime:
  name: plan-cli-ambiguous
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!shared:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
routes:
  fanout:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    channel_room_map:
      0: '!shared:fake.local'
      1: '!shared:fake.local'
"""

# Meshtastic source + source_to_dest + duplicate rooms → fan-in (allowed).
_CONFIG_SAME_ROOM_FANIN = """\
runtime:
  name: plan-cli-fanin
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!shared:fake.local']
      encryption_mode: plaintext
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
routes:
  fanin:
    source_adapters: [radio]
    dest_adapters: [main]
    directionality: source_to_dest
    channel_room_map:
      0: '!shared:fake.local'
      1: '!shared:fake.local'
"""

# Route with no per-entry or route-level labels so the plan applies the
# source adapter's origin_label as the effective label (adapter fallback).
_CONFIG_ADAPTER_FALLBACK = """\
runtime:
  name: plan-cli-adapter-fallback
storage:
  backend: memory
adapters:
  matrix:
    main:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: tok
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
      origin_label: AdapterMatrix
  meshtastic:
    radio:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: AdapterMesh
routes:
  bridge:
    source_adapters: [main]
    dest_adapters: [radio]
    directionality: source_to_dest
    source_room: '!room:fake.local'
    dest_channel: '1'
"""

# Config with a YAML parse error (malformed indentation).
_CONFIG_PARSE_ERROR = """\
runtime:
  name: plan-cli-bad
storage
  backend: memory
"""

# Truly minimal config.
_CONFIG_MINIMAL = "runtime: {}\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, yaml_text: str, name: str = "config.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml_text)
    return p


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure MEDRE_HOME / MEDRE_CONFIG do not leak from the environment."""
    for var in ("MEDRE_HOME", "MEDRE_CONFIG"):
        monkeypatch.delenv(var, raising=False)


# ===========================================================================
# 1. Valid config prints plan → exit 0
# ===========================================================================


def test_valid_config_prints_plan(tmp_path: Path) -> None:
    """A valid config exits 0 and prints the plan header and route IDs."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Route Plan (offline" in stdout
    assert "bridge" in stdout
    assert "paused" in stdout
    assert "main" in stdout
    assert "radio" in stdout


def test_valid_config_lists_adapters(tmp_path: Path) -> None:
    """The plan prints an Adapters section with each adapter."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Adapters (2):" in stdout
    assert "matrix:main" in stdout
    assert "meshtastic:radio" in stdout


def test_valid_config_lists_legs(tmp_path: Path) -> None:
    """The plan prints per-leg detail under each enabled route."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Leg 1:" in stdout
    # The bidirectional bridge produces 2 legs.
    assert "2 leg(s)" in stdout


# ===========================================================================
# 2. Plan includes origin-label provenance
# ===========================================================================


def test_plan_includes_origin_label_provenance(tmp_path: Path) -> None:
    """Each leg prints an origin_label line with its provenance source."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "origin_label:" in stdout
    # The route-level labels appear in the provenance annotation.
    assert "MatrixSide" in stdout
    assert "MeshSide" in stdout
    assert "(route)" in stdout


# ===========================================================================
# 3. No routes → plan shows empty
# ===========================================================================


def test_no_routes_plan_exits_zero(tmp_path: Path) -> None:
    """A config with no routes exits 0 and shows the empty-plan marker."""
    cfg = _write_config(tmp_path, _CONFIG_NO_ROUTES)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Routes (0 configured, 0 legs):" in stdout
    assert "(none configured)" in stdout


def test_minimal_config_plan_exits_zero(tmp_path: Path) -> None:
    """A truly minimal config (runtime: {}) also produces an empty plan."""
    cfg = _write_config(tmp_path, _CONFIG_MINIMAL)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Route Plan" in stdout
    assert "(none configured)" in stdout


# ===========================================================================
# 4. Disabled routes shown
# ===========================================================================


def test_disabled_routes_listed_separately(tmp_path: Path) -> None:
    """Disabled routes appear in a dedicated 'Disabled routes' section."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "Disabled routes (1):" in stdout
    assert "paused [source_to_dest] [OFF]" in stdout


def test_disabled_routes_have_no_legs_section(tmp_path: Path) -> None:
    """Disabled routes do not contribute legs to the enabled-routes section."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    # The 'bridge' route is bidirectional → 2 legs total, and 'paused' adds 0.
    assert "2 leg(s)" in stdout
    # The disabled route's ID appears under 'Disabled routes', not as an
    # enabled entry with legs.
    assert "paused [source_to_dest] [OFF]" in stdout


# ===========================================================================
# 5. Unknown adapter reference fails → exit nonzero, no traceback
# ===========================================================================


def test_unknown_adapter_fails_nonzero(tmp_path: Path) -> None:
    """An enabled route referencing an unknown adapter exits nonzero."""
    cfg = _write_config(tmp_path, _CONFIG_UNKNOWN_ADAPTER)
    stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    # The error is reported on the plan (route entry carries an error),
    # so stdout includes the route and the error marker.
    assert "broken" in stdout
    assert "nonexistent" in stdout + stderr


def test_unknown_adapter_no_traceback(tmp_path: Path) -> None:
    """Unknown-adapter failures produce a clean message, not a traceback."""
    cfg = _write_config(tmp_path, _CONFIG_UNKNOWN_ADAPTER)
    _stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    assert "Traceback" not in stderr
    assert "Traceback" not in _stdout


# ===========================================================================
# 6. Duplicate-room ambiguity fails → exit nonzero
# ===========================================================================


def test_duplicate_room_ambiguity_fails(tmp_path: Path) -> None:
    """A Matrix→Meshtastic route with duplicate rooms fails (ambiguous)."""
    cfg = _write_config(tmp_path, _CONFIG_DUPLICATE_ROOM_AMBIGUOUS)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    assert "fanout" in stdout
    # The error mentions the duplicate room and the ambiguity.
    combined = stdout
    assert "!shared:fake.local" in combined


def test_duplicate_room_ambiguity_explains_problem(tmp_path: Path) -> None:
    """The error message explains the Matrix→Meshtastic ambiguity."""
    cfg = _write_config(tmp_path, _CONFIG_DUPLICATE_ROOM_AMBIGUOUS)
    stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    combined = stdout + stderr
    # The ambiguity error references the route and the duplicate room.
    assert "fanout" in combined
    assert "shared" in combined


# ===========================================================================
# 7. Same-room fan-in allowed → plan succeeds with fan-in warning
# ===========================================================================


def test_same_room_fanin_allowed(tmp_path: Path) -> None:
    """Meshtastic→Matrix fan-in with a shared room is allowed (exit 0)."""
    cfg = _write_config(tmp_path, _CONFIG_SAME_ROOM_FANIN)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "fanin" in stdout
    assert "2 leg(s)" in stdout


def test_same_room_fanin_emits_warning(tmp_path: Path) -> None:
    """The fan-in case emits a fan-in annotation under the route."""
    cfg = _write_config(tmp_path, _CONFIG_SAME_ROOM_FANIN)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "fan-in" in stdout
    assert "!shared:fake.local" in stdout


# ===========================================================================
# 8. Config parse error → clean error, no traceback
# ===========================================================================


def test_parse_error_exits_nonzero(tmp_path: Path) -> None:
    """A YAML parse error exits nonzero."""
    cfg = _write_config(tmp_path, _CONFIG_PARSE_ERROR)
    _stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    assert "Config error:" in stderr


def test_parse_error_no_traceback(tmp_path: Path) -> None:
    """A parse error produces a clean message, not a traceback."""
    cfg = _write_config(tmp_path, _CONFIG_PARSE_ERROR)
    _stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    assert "Traceback" not in stderr


# ===========================================================================
# 9. `.toml` config rejected → exit nonzero, migration message
# ===========================================================================


def test_toml_config_rejected(tmp_path: Path) -> None:
    """A .toml config is rejected with the migration message."""
    cfg = _write_config(
        tmp_path,
        "runtime = { name = 'toml' }\n",
        name="config.toml",
    )
    _stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code != 0
    assert "TOML" in stderr
    assert "YAML" in stderr


def test_toml_config_no_traceback(tmp_path: Path) -> None:
    """The TOML rejection does not leak a traceback."""
    cfg = _write_config(
        tmp_path,
        "runtime = { name = 'toml' }\n",
        name="config.toml",
    )
    _stdout, stderr, _code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert "Traceback" not in stderr


# ===========================================================================
# 10. No secrets in output
# ===========================================================================


def test_access_token_not_in_output(tmp_path: Path) -> None:
    """The Matrix access_token never appears in plan output."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    secret = "s3cret-token-do-not-leak"
    assert secret not in stdout
    assert secret not in stderr


def test_access_token_not_in_json_output(tmp_path: Path) -> None:
    """The Matrix access_token is absent from --json output too."""
    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, stderr, code = _run_cli_raw(
        "routes", "plan", "--config", str(cfg), "--json"
    )
    assert code == 0
    secret = "s3cret-token-do-not-leak"
    assert secret not in stdout
    assert secret not in stderr
    assert secret not in stderr


# ===========================================================================
# JSON output mode
# ===========================================================================


def test_json_mode_emits_valid_json(tmp_path: Path) -> None:
    """The --json flag emits parseable JSON with the expected top-level keys."""
    import json

    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw(
        "routes", "plan", "--config", str(cfg), "--json"
    )
    assert code == 0
    data = json.loads(stdout)
    assert "adapters" in data
    assert "routes" in data
    assert "total_legs" in data
    assert "loops" in data
    assert len(data["routes"]) == 2  # bridge + paused


def test_json_mode_includes_provenance(tmp_path: Path) -> None:
    """JSON output carries source_origin_label_source per leg."""
    import json

    cfg = _write_config(tmp_path, _CONFIG_VALID)
    stdout, _stderr, code = _run_cli_raw(
        "routes", "plan", "--config", str(cfg), "--json"
    )
    assert code == 0
    data = json.loads(stdout)
    bridge = next(r for r in data["routes"] if r["route_id"] == "bridge")
    sources = {leg["source_origin_label_source"] for leg in bridge["legs"]}
    assert sources == {"route"}


# ===========================================================================
# 11. Adapter-level origin_label fallback in plan output
# ===========================================================================


def test_plan_text_shows_adapter_provenance(tmp_path: Path) -> None:
    """Plan text output reports the adapter fallback provenance.

    With no per-entry or route-level label, the plan applies the source
    adapter's origin_label as the effective label and annotates the leg
    with '(adapter)'.
    """
    cfg = _write_config(tmp_path, _CONFIG_ADAPTER_FALLBACK)
    stdout, _stderr, code = _run_cli_raw("routes", "plan", "--config", str(cfg))
    assert code == 0
    assert "AdapterMatrix" in stdout
    assert "(adapter)" in stdout


def test_plan_json_shows_adapter_provenance(tmp_path: Path) -> None:
    """JSON output reports source_origin_label_source='adapter' per leg.

    With no per-entry or route-level label, the source adapter's
    origin_label becomes the effective label and the leg carries
    source_origin_label_source='adapter'.
    """
    import json

    cfg = _write_config(tmp_path, _CONFIG_ADAPTER_FALLBACK)
    stdout, _stderr, code = _run_cli_raw(
        "routes", "plan", "--config", str(cfg), "--json"
    )
    assert code == 0
    data = json.loads(stdout)
    bridge = next(r for r in data["routes"] if r["route_id"] == "bridge")
    assert len(bridge["legs"]) == 1
    leg = bridge["legs"][0]
    assert leg["source_origin_label"] == "AdapterMatrix"
    assert leg["source_origin_label_source"] == "adapter"
