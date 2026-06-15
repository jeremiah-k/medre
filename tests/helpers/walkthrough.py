"""Shared helpers for walkthrough CLI test modules.

Extracted from the original walkthrough CLI test monolith.
Contains constants, YAML config templates, seed helpers, and the
autouse path-cleanup fixture used across the split test files.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import yaml

from medre.cli import main

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIR = REPO_ROOT / "src"
EXAMPLES_SMOKE_CONFIG = REPO_ROOT / "examples" / "configs" / "fake-bridge-smoke.yaml"

# Optional SDK module names (import names and fork import names) that must
# NOT appear in sys.modules after fake-only CLI operations.
OPTIONAL_SDK_MODULES: frozenset[str] = frozenset(
    {
        "nio",
        "mindroom_nio",
        "meshtastic",
        "mtjk",
        "meshcore",
        "meshcore_py",
        "RNS",
        "LXMF",
        "lxmf",
    }
)


def optional_sdks_in_modules() -> set[str]:
    """Return which optional SDK modules are currently in sys.modules."""
    return {m for m in OPTIONAL_SDK_MODULES if m in sys.modules}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def smoke_config_path() -> str:
    """Return path to the shipped fake-bridge-smoke.yaml."""
    from medre.runtime.smoke import _default_smoke_config_path

    path = _default_smoke_config_path()
    assert path is not None, "examples/configs/fake-bridge-smoke.yaml not found"
    return path


# YAML config with SQLite storage for replay tests.
REPLAY_YAML = """\
runtime:
  name: alpha-replay-walkthrough
  shutdown_timeout_seconds: 10
logging:
  level: WARNING
  format: text
storage:
  backend: sqlite
  path: '{storage_path}'
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bot:fake.local'
      access_token: fake
      room_allowlist: ['!room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: alpha-walkthrough
routes:
  mx_to_mesh:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_meshtastic]
    directionality: source_to_dest
    enabled: true
"""

# Minimal YAML config with SQLite storage for smoke seeding.
SMOKE_STORAGE_YAML = """\
runtime:
  name: fake-bridge-smoke-persist
  shutdown_timeout_seconds: 10
logging:
  level: WARNING
  format: text
storage:
  backend: sqlite
  path: '{storage_path}'
adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: '@bridge-bot:fake.local'
      access_token: fake_token_bridge_smoke
      room_allowlist: ['!bridge-room:fake.local']
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: smoke-radio
routes:
  mx_to_mesh:
    source_adapters: [fake_matrix]
    dest_adapters: [fake_meshtastic]
    directionality: source_to_dest
    enabled: true
"""


def write_replay_config(tmp_path: Path, db_path: Path) -> str:
    """Write a YAML config that points storage at *db_path* for replay."""
    cfg = tmp_path / "replay_config.yaml"
    cfg.write_text(REPLAY_YAML.format(storage_path=str(db_path)))
    return str(cfg)


def write_smoke_storage_config(tmp_path: Path, db_path: Path) -> str:
    """Write a YAML config with SQLite storage at *db_path* for smoke tests.

    Use this when a smoke test needs to persist evidence for post-run
    inspection (read-only commands like ``inspect``, ``trace``, ``evidence``,
    ``recover`` all use ``--storage-path`` against the resulting DB file).
    """
    cfg = tmp_path / "smoke_storage_config.yaml"
    cfg.write_text(SMOKE_STORAGE_YAML.format(storage_path=str(db_path)))
    return str(cfg)


def write_sqlite_config_from_example(tmp_path: Path, db_path: Path) -> str:
    """Derive a SQLite YAML config from the shipped fake-bridge-smoke.yaml.

    Reads the shipped example, replaces ``storage.backend: memory`` with
    SQLite at *db_path*, and writes a derived YAML config preserving the
    complete route topology and adapter set.
    """
    assert (
        EXAMPLES_SMOKE_CONFIG.is_file()
    ), f"Source-tree example config not found: {EXAMPLES_SMOKE_CONFIG}"
    data = yaml.safe_load(EXAMPLES_SMOKE_CONFIG.read_text(encoding="utf-8"))

    storage = data.setdefault("storage", {})
    assert storage.get("backend") == "memory", (
        "Expected storage.backend == 'memory' in example config, "
        f"got {storage.get('backend')!r}"
    )
    storage["backend"] = "sqlite"
    storage["path"] = str(db_path)

    derived = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)
    cfg = tmp_path / "smoke_sqlite_from_example.yaml"
    cfg.write_text(derived, encoding="utf-8")
    return str(cfg)


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def seed_via_smoke_cli(tmp_path: Path) -> tuple[str, Path]:
    """Run ``main(["smoke", ...])`` to create a populated DB.

    Returns (event_id, db_path).
    """
    db_path = tmp_path / "walkthrough.db"
    config_path = write_smoke_storage_config(tmp_path, db_path)

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "smoke",
                    "--config",
                    config_path,
                    "--json",
                ]
            )
    assert exc_info.value.code == 0, (
        f"Smoke seed failed (exit={exc_info.value.code}): "
        f"stderr={stderr_buf.getvalue()}"
    )
    report = json.loads(stdout_buf.getvalue())
    assert (
        report["status"] == "passed"
    ), f"Smoke report not passed: {report.get('fail_reasons', [])}"
    event_id = report["event_id"]
    assert isinstance(event_id, str) and len(event_id) > 0

    return event_id, db_path


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_path_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear MEDRE_HOME and XDG env vars so tests get clean state."""
    for var in (
        "MEDRE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
