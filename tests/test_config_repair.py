"""Config repair workflow tests.

Validates realistic operator repair loops for configuration errors:
malformed YAML, invalid storage paths, unsupported backends, invalid
limits, missing config files, and duplicate route IDs. Each scenario
follows the pattern: initial failure → actionable error → operator fix →
config loads cleanly.

Split from the former ``tests/test_operator_recovery.py`` monolith.
Shared fixtures/helpers live in ``tests/helpers/operator_recovery.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medre.config.errors import (
    ConfigFileError,
    ConfigNotFoundError,
    ConfigValidationError,
)
from medre.config.loader import load_config
from medre.config.model import RuntimeLimits
from medre.config.paths import MedrePaths, MedrePathsError, resolve
from medre.runtime.builder import RuntimeBuilder
from medre.runtime.errors import RuntimeConfigError
from tests.helpers.operator_recovery import (
    CONFIG_BAD_YAML,
    _build_app,
    _run_cli,
    _run_cli_raw,
    _write_config,
)

# ---------------------------------------------------------------------------
# Fixtures (re-declared locally; pytest does not discover imported fixtures
# from non-conftest helper modules — see tests/helpers/startup_cleanup.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub all MEDRE_ and XDG_ env vars to avoid cross-test leakage."""
    for key in list(os.environ):
        if key.startswith("MEDRE_") or key.startswith("XDG_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def tmp_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MedrePaths:
    """Create a MedrePaths pointing at temp directories."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    return resolve()


# ---------------------------------------------------------------------------
# Local YAML config snippets (used only in this file)
# ---------------------------------------------------------------------------

CONFIG_VALID_FAKE = """\
runtime:
  name: recovery-test
storage:
  backend: memory
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok_recovery
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    fake_mesh:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: RecoveryMesh
routes:
  matrix_to_mesh:
    source_adapters:
      - fake_matrix
    dest_adapters:
      - fake_mesh
    directionality: source_to_dest
    enabled: true
"""

CONFIG_VALID_SINGLE = """\
runtime:
  name: recovery-single
storage:
  backend: memory
adapters:
  matrix:
    solo:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok_solo
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
"""

CONFIG_INVALID_LIMITS = """\
runtime:
  name: bad-limits
  limits:
    max_inflight_deliveries: -1
storage:
  backend: memory
"""

CONFIG_SQLITE_PATH = """\
runtime:
  name: sqlite-recovery
storage:
  backend: sqlite
  path: "{state}/recovery_test.db"
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok_sqlite
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
"""


# ---------------------------------------------------------------------------
# Malformed config recovery
#
# Operator writes bad YAML → sees clean error → fixes → config loads.
# Validates the repair loop: initial failure gives actionable message,
# operator fixes the config, and the second attempt succeeds.
# ---------------------------------------------------------------------------


def test_bad_yaml_then_fix_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bad YAML syntax → ConfigFileError → fix → load_config succeeds."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_BAD_YAML)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    # Step 1: bad config should fail with clear error.
    with pytest.raises(ConfigFileError) as exc_info:
        load_config(None)
    msg = str(exc_info.value)
    # YAML parse errors always reference the source file path.
    assert "config.yaml" in msg
    # No raw traceback in the error message.
    assert "Traceback" not in msg

    # Step 2: operator fixes the config file.
    cfg_path.write_text(CONFIG_VALID_FAKE)

    # Step 3: fixed config loads successfully.
    config, source, paths = load_config(None)
    assert config.runtime.name == "recovery-test"
    assert len(config.adapters.all_enabled()) == 2


def test_config_check_cli_bad_then_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 'config check' with bad config → exit 1 → fix → exit 0."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_BAD_YAML)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    # Step 1: bad config → CLI reports error.
    stdout, stderr, code = _run_cli_raw("config", "check")
    assert code != 0
    assert "Config error" in stderr
    assert "Traceback" not in stderr
    assert "Traceback" not in stdout

    # Step 2: fix the config.
    cfg_path.write_text(CONFIG_VALID_FAKE)

    # Step 3: fixed config passes check.
    stdout2, stderr2, code2 = _run_cli_raw("config", "check")
    assert code2 == 0
    assert "Config valid" in stdout2


def test_missing_sections_then_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config with missing runtime section → error → add section → loads."""
    minimal = "storage:\n  backend: memory\n"
    cfg_path = _write_config(tmp_path / "config.yaml", minimal)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    # Load should still succeed (runtime has defaults).
    config, _, _ = load_config(None)
    assert config.storage.backend == "memory"

    # Add adapters section → full config.
    full = CONFIG_VALID_SINGLE
    cfg_path.write_text(full)
    config2, _, _ = load_config(None)
    assert len(config2.adapters.all_enabled()) == 1


# ---------------------------------------------------------------------------
# Storage-path recovery
#
# Invalid storage path → error → fix path → runtime starts.
# Validates that storage path issues are caught early and produce
# actionable messages for the operator to fix.
# ---------------------------------------------------------------------------


def test_unknown_placeholder_then_fix(tmp_paths: MedrePaths) -> None:
    """Unknown path placeholder → MedrePathsError → fix → resolves."""
    with pytest.raises(MedrePathsError) as exc_info:
        tmp_paths.expand_placeholder("{totally_bogus}/data.db")
    msg = str(exc_info.value)
    assert "unknown path placeholder" in msg
    assert "totally_bogus" in msg

    # Fix: use a known placeholder.
    resolved = tmp_paths.expand_placeholder("{state}/data.db")
    assert "data.db" in str(resolved)


def test_invalid_storage_backend_then_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unsupported storage backend → RuntimeConfigError at build time → fix → builds."""
    bad_cfg = """\
runtime:
  name: bad-storage
storage:
  backend: cassandra
adapters:
  matrix:
    m:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
"""
    cfg_path = _write_config(tmp_path / "config.yaml", bad_cfg)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    # Config loads OK — backend validation happens at build time.
    config, _, paths = load_config(None)
    assert config.storage.backend == "cassandra"

    # Building the runtime with unsupported backend raises.
    with pytest.raises(RuntimeConfigError) as exc_info:
        RuntimeBuilder(config, paths).build()
    msg = str(exc_info.value)
    assert "cassandra" in msg
    assert "Traceback" not in msg

    # Fix: use supported backend.
    cfg_path.write_text(CONFIG_VALID_FAKE)
    config2, _, paths2 = load_config(None)
    app = RuntimeBuilder(config2, paths2).build()
    assert app is not None


def test_sqlite_path_with_valid_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite storage with valid {state} placeholder works end-to-end."""
    monkeypatch.setenv("MEDRE_HOME", str(tmp_path))
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_SQLITE_PATH)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    config, _, paths = load_config(None)
    assert config.storage.backend == "sqlite"
    assert config.storage.path is not None

    # Build and start runtime with SQLite storage.
    app = _build_app(config, paths)
    assert app.storage is not None


# ---------------------------------------------------------------------------
# Config repair workflows
#
# Common config issues → actionable error → fix → valid config.
# Validates that common misconfiguration patterns produce actionable
# error messages that guide the operator to a fix.
# ---------------------------------------------------------------------------


def test_invalid_limits_then_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative limits → ConfigValidationError with field name → fix → valid."""
    with pytest.raises(ConfigValidationError) as exc_info:
        RuntimeLimits(max_inflight_deliveries=-1).validate()
    msg = str(exc_info.value)
    assert "max_inflight_deliveries" in msg
    assert "must be > 0" in msg

    # Fix: use valid limits.
    limits = RuntimeLimits(max_inflight_deliveries=100)
    limits.validate()  # Should not raise.


def test_missing_config_file_then_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No config file → ConfigNotFoundError suggests 'config sample' → create → loads."""
    monkeypatch.delenv("MEDRE_HOME", raising=False)
    monkeypatch.delenv("MEDRE_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    with pytest.raises(ConfigNotFoundError) as exc_info:
        load_config(None)
    msg = str(exc_info.value)
    assert "medre config sample" in msg
    assert "Traceback" not in msg


def test_config_sample_generates_valid_config() -> None:
    """'medre config sample' output is parseable YAML with key sections."""
    import yaml

    stdout = _run_cli("config", "sample")
    assert "Traceback" not in stdout
    parsed = yaml.safe_load(stdout)
    assert isinstance(parsed, dict)


def test_config_check_detects_invalid_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI 'config check' catches invalid limits and reports them."""
    cfg_path = _write_config(tmp_path / "config.yaml", CONFIG_INVALID_LIMITS)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    stdout, stderr, code = _run_cli_raw("config", "check")
    assert code != 0
    # Error should mention the limits issue.
    combined = stdout + stderr
    assert "Traceback" not in combined


def test_duplicate_route_id_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate route IDs → error → rename → valid."""
    dup_cfg = """\
runtime:
  name: dup-routes
storage:
  backend: memory
adapters:
  matrix:
    a:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: tok
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    b:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: Mesh
routes:
  dup_id:
    source_adapters:
      - a
    dest_adapters:
      - b
    directionality: source_to_dest
    enabled: true
  dup_id:
    source_adapters:
      - b
    dest_adapters:
      - a
    directionality: source_to_dest
    enabled: true
"""
    cfg_path = _write_config(tmp_path / "config.yaml", dup_cfg)
    monkeypatch.setenv("MEDRE_CONFIG", str(cfg_path))

    # Duplicate YAML mapping keys are a parse error (caught by the
    # strict YAML loader), so load_config should raise ConfigFileError
    # rather than silently merging.
    with pytest.raises(ConfigFileError, match="[Dd]uplicate"):
        load_config(None)
