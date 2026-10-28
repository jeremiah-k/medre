"""Native POSIX store names preserve containment and host-representability checks."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import jsonschema
import pytest

from medre.maintenance import stopped_snapshot as snapshot


def test_posix_store_name_roundtrips_without_renaming(tmp_path):
    if os.name == "nt":
        pytest.skip("native colon filenames require POSIX")
    root = tmp_path / "state"
    root.mkdir()
    database = root / "@relay:example.org_DEVICE.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('identity')")
    entry = snapshot.SnapshotEntry(database.name, "core_sqlite", "sqlite")
    archive = tmp_path / "snapshot"
    snapshot.create_stopped_snapshot(root, archive, [entry], runtime_stopped=True)
    schema = json.loads(
        Path("docs/schemas/stopped-state-snapshot.schema.json").read_text()
    )
    jsonschema.validate(json.loads((archive / "manifest.json").read_text()), schema)
    restored = tmp_path / "restored"
    snapshot.restore_stopped_snapshot(archive, restored, runtime_stopped=True)
    with closing(sqlite3.connect(restored / database.name)) as connection:
        assert connection.execute("SELECT value FROM retained").fetchone() == (
            "identity",
        )


@pytest.mark.parametrize(
    "path", ["C:keys", "store/C:keys", r"C:\keys", "../key", "/key", "store//key"]
)
def test_native_names_do_not_admit_drive_or_traversal_spelling(path):
    with pytest.raises(ValueError):
        snapshot.SnapshotEntry(path, "native_identity", "opaque")


def test_windows_representability_refuses_stream_before_file_access(monkeypatch):
    monkeypatch.setattr(snapshot, "_NATIVE_COLONS", False)
    with pytest.raises(ValueError):
        snapshot.SnapshotEntry(
            "store/@relay:example.org_DEVICE.db", "native_identity", "sqlite"
        )


def test_incompatible_later_entry_refuses_before_any_selected_file_opens(
    tmp_path, monkeypatch
):
    archive = tmp_path / "snapshot"
    archive.mkdir()
    manifest = archive / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "format": "medre-stopped-state",
                "version": 1,
                "files": [
                    {
                        "path": "core.sqlite",
                        "role": "core_sqlite",
                        "format": "sqlite",
                        "bytes": 0,
                        "sha256": "0" * 64,
                    },
                    {
                        "path": "store/@relay:example.org_DEVICE.db",
                        "role": "native_identity",
                        "format": "sqlite",
                        "bytes": 0,
                        "sha256": "0" * 64,
                    },
                ],
            }
        )
    )
    monkeypatch.setattr(snapshot, "_NATIVE_COLONS", False)
    original = snapshot._open_regular

    def manifest_only(path):
        assert path == manifest, "selected file opened before complete path validation"
        return original(path)

    monkeypatch.setattr(snapshot, "_open_regular", manifest_only)
    with pytest.raises(ValueError, match="safe relative"):
        snapshot.verify_stopped_snapshot(archive)
