# medre-test-tier: compatibility
"""Adapter-backed smoke fixtures for exact-target replay CLI execution.

These tests exercise fake transport registration and belong to the adapter
compatibility tier. The operator decision and malformed-input tests remain
in the adapter-free core suite.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from medre.config import loader as _config_loader
from tests.helpers.cli import _replay_cli


@pytest.fixture(autouse=True)
def _declared_replay_transports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declare the transports the replay fixtures name.

    Transport-group validation reads the import-time snapshot of installed
    adapters, which is empty in adapter-free environments. The fixtures
    name these groups themselves, mirroring the bridge compatibility
    suites that declare the transports their configs use.
    """
    declared = frozenset({"matrix", "meshtastic", "fake_meshtastic"})
    monkeypatch.setattr(
        _config_loader,
        "_KNOWN_TRANSPORTS",
        declared | frozenset(_config_loader._KNOWN_TRANSPORTS),
    )


_REPLAY_YAML = """\
runtime:
  name: exact-target-replay-surface
  shutdown_timeout_seconds: 10

logging:
  level: WARNING
  format: text

storage:
  backend: sqlite
  path: "{storage_path}"

adapters:
  matrix:
    fake_matrix:
      enabled: true
      adapter_kind: fake
      homeserver: https://fake.local
      user_id: "@bot:fake.local"
      access_token: fake
      room_allowlist:
        - "!room:fake.local"
      encryption_mode: plaintext
  meshtastic:
    fake_meshtastic:
      enabled: true
      adapter_kind: fake
      connection_type: fake
      origin_label: exact-target-surface

routes:
  mx_to_mesh:
    source_adapters:
      - fake_matrix
    dest_adapters:
      - fake_meshtastic
    directionality: source_to_dest
    enabled: true
"""


def _seed_replay_db(tmp_path: Path) -> tuple[str, Path]:
    db_path = tmp_path / "exact_target_replay.db"
    config_path = tmp_path / "exact_target_replay.yaml"
    config_path.write_text(_REPLAY_YAML.format(storage_path=str(db_path)))
    code, stdout, stderr = _replay_cli(
        "smoke",
        "--config",
        str(config_path),
        "--json",
    )
    assert code == 0, f"smoke seed failed: {stderr}; stdout: {stdout[:2000]}"
    report = json.loads(stdout)
    assert report["status"] == "passed", f"smoke seed report: {stdout[:2000]}"
    return report["event_id"], config_path


def test_dry_run_accepts_exact_target_selections(tmp_path: Path):
    event_id, config_path = _seed_replay_db(tmp_path)
    code, stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "dry_run",
        "--event",
        event_id,
        "--json",
        "--exact-target",
        "evt:plan:adapter:!room:server",
        "--exact-no-channel",
        "evt2:plan2:adapter2",
    )
    assert code == 0, f"dry_run failed: {stderr}"
    summary = json.loads(stdout)
    assert summary["mode"] == "dry_run"
    # DRY_RUN walks the five replay stages for the one seeded event, even
    # though the supplied exact identities match no staged delivery. Exact
    # selections gate delivery admission only; dry_run does not predict
    # which exact targets a BEST_EFFORT replay would authorize.
    assert summary["events_scanned"] == 1
    assert summary["by_stage"] == {
        "store": 1,
        "route": 1,
        "plan": 1,
        "render": 1,
        "deliver": 1,
    }
    assert summary["events_replayed"] == 5


def test_best_effort_admits_nothing_outside_the_named_selection(tmp_path: Path):
    event_id, config_path = _seed_replay_db(tmp_path)
    db_path = tmp_path / "exact_target_replay.db"
    code, stdout, stderr = _replay_cli(
        "replay",
        "--config",
        str(config_path),
        "--mode",
        "best_effort",
        "--event",
        event_id,
        "--json",
        "--exact-target",
        "absent:plan:adapter:!room:server",
    )
    assert code == 0, f"best_effort failed: {stderr}"
    summary = json.loads(stdout)
    assert summary["mode"] == "best_effort"
    # The named identity matches no persisted delivery plan, so delivery
    # admission admits no new generation for the seeded event.
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM delivery_outbox WHERE event_id=?", (event_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 1
