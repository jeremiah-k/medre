"""A stopped installation is restored as one verified, non-overwriting unit."""

from __future__ import annotations

import errno
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from medre.maintenance.stopped_snapshot import (
    SnapshotEntry,
    create_stopped_snapshot,
    restore_stopped_snapshot,
    verify_stopped_snapshot,
)


def _symlink_or_skip(link: Path, target: Path) -> None:
    """Skip only when this platform cannot construct the symlink fixture."""
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except NotImplementedError:
        pytest.skip("symlink creation is unavailable")
    except OSError as exc:
        if exc.errno not in {
            errno.EPERM,
            errno.EACCES,
            errno.ENOSYS,
            errno.ENOTSUP,
        } and getattr(exc, "winerror", None) not in {1, 50, 1314}:
            raise
        pytest.skip("symlink creation is unavailable")


def _source(tmp_path: Path):
    """Select one core database, an opaque provider journal and binary native identity."""
    root = tmp_path / "installation"
    (root / "state").mkdir(parents=True)
    (root / "native").mkdir()
    (root / "adapters").mkdir()
    database = root / "state" / "medre.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO facts VALUES (1, 'before restart')")
    conn.commit()
    conn.close()
    (root / "native" / "identity.key").write_bytes(b"private-key-data")
    (root / "adapters" / "ledger.db").write_bytes(b"adapter-ledger")
    entries = [
        SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite"),
        SnapshotEntry("native/identity.key", "native_identity", "opaque"),
        SnapshotEntry("adapters/ledger.db", "adapter_ledger", "opaque"),
    ]
    return root, entries


def test_snapshot_and_fresh_restore_preserve_all_selected_files(tmp_path):
    """A complete selected snapshot reproduces core records and exact opaque state."""
    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    restored = tmp_path / "restored"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    assert set(verify_stopped_snapshot(snapshot)) == set(entries)
    restore_stopped_snapshot(snapshot, restored, runtime_stopped=True)
    for entry in entries:
        original, final = root / entry.path, restored / entry.path
        if entry.role == "core_sqlite":
            conn = sqlite3.connect(final)
            assert conn.execute("SELECT value FROM facts").fetchone() == (
                "before restart",
            )
            conn.close()
        else:
            assert original.read_bytes() == final.read_bytes()


def test_snapshot_refuses_running_runtime_and_existing_destination(tmp_path):
    """Explicit maintenance consent never permits replacing an existing installation."""
    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="stop the runtime"):
        create_stopped_snapshot(root, snapshot, entries)
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    with pytest.raises(FileExistsError):
        create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    with pytest.raises(FileExistsError):
        restore_stopped_snapshot(snapshot, root, runtime_stopped=True)


@pytest.mark.parametrize("change", ["bytes", "unexpected", "manifest", "symlink"])
def test_verification_blocks_corruption_or_extra_files(tmp_path, change):
    """Changed content and unselected paths prevent any restored directory publication."""
    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    if change == "bytes":
        (snapshot / "files/native/identity.key").write_bytes(b"altered")
    elif change == "unexpected":
        (snapshot / "files" / "unknown").write_text("extra")
    elif change == "manifest":
        data = json.loads((snapshot / "manifest.json").read_text())
        data["files"][0]["path"] = "../escaped"
        (snapshot / "manifest.json").write_text(json.dumps(data))
    else:
        key = snapshot / "files/native/identity.key"
        key.unlink()
        _symlink_or_skip(key, root / "native/identity.key")
    with pytest.raises((ValueError, OSError)):
        restore_stopped_snapshot(snapshot, tmp_path / "target", runtime_stopped=True)
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "path", ["../secret", "/etc/passwd", "native/./key", "native//key"]
)
def test_snapshot_rejects_path_escape_or_noncanonical_path(tmp_path, path):
    """A selected path must have one canonical spelling beneath the installation root."""
    root, entries = _source(tmp_path)
    with pytest.raises(ValueError):
        entries[1] = SnapshotEntry(path, "native_identity", "opaque")
        create_stopped_snapshot(
            root, tmp_path / "snapshot", entries, runtime_stopped=True
        )


def test_snapshot_rejects_source_symlink(tmp_path):
    """Selected native state cannot redirect capture through a filesystem symlink."""
    root, entries = _source(tmp_path)
    (root / "native/identity.key").unlink()
    _symlink_or_skip(root / "native/identity.key", root / "adapters/ledger.db")
    with pytest.raises(ValueError, match="symlinks"):
        create_stopped_snapshot(
            root, tmp_path / "snapshot", entries, runtime_stopped=True
        )


async def test_restored_medre_storage_reopens_with_retained_event(tmp_path):
    """The copied core database must satisfy MEDRE's actual storage contracts."""
    from datetime import datetime, timezone

    from medre.core.events import CanonicalEvent, EventMetadata
    from medre.core.storage.sqlite.storage import SQLiteStorage

    root = tmp_path / "running-root"
    (root / "state").mkdir(parents=True)
    (root / "native").mkdir()
    (root / "native" / "profile.key").write_bytes(b"selected-identity")
    original = SQLiteStorage(db_path=str(root / "state" / "medre.sqlite"))
    await original.initialize()
    await original.append(
        CanonicalEvent(
            event_id="retained-1",
            event_kind="message.created",
            schema_version=1,
            timestamp=datetime.now(timezone.utc),
            source_adapter="fixture",
            source_transport_id="node",
            source_channel_id="0",
            parent_event_id=None,
            lineage=(),
            relations=(),
            payload={"text": "hello"},
            metadata=EventMetadata(),
        )
    )
    await original.close()
    snapshot = tmp_path / "archive"
    create_stopped_snapshot(
        root,
        snapshot,
        [
            SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite"),
            SnapshotEntry("native/profile.key", "native_identity", "opaque"),
        ],
        runtime_stopped=True,
    )
    restored = tmp_path / "new-root"
    restore_stopped_snapshot(snapshot, restored, runtime_stopped=True)
    storage = SQLiteStorage(db_path=str(restored / "state/medre.sqlite"))
    await storage.initialize()
    try:
        assert await storage.count_events() == 1
    finally:
        await storage.close()
    assert (restored / "native/profile.key").read_bytes() == b"selected-identity"


def test_restore_rejects_bytes_changed_after_verification(tmp_path, monkeypatch):
    """Publication must use the verified digest, not a second mutable source read."""
    from medre.maintenance import stopped_snapshot as subject

    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    key = snapshot / "files/native/identity.key"
    digest = subject._digest
    changed = False

    def change_after_hash(path):
        """Replace native bytes immediately after their verified digest has been captured."""
        nonlocal changed
        result = digest(path)
        if path == key and not changed:
            changed = True
            key.write_bytes(b"replacement-after-verification")
        return result

    monkeypatch.setattr(subject, "_digest", change_after_hash)
    destination = tmp_path / "restored"
    with pytest.raises(ValueError, match="checksum|changed"):
        restore_stopped_snapshot(snapshot, destination, runtime_stopped=True)
    assert changed and not destination.exists()


@pytest.mark.parametrize("role", ["adapter_ledger", "native_identity"])
def test_selected_sqlite_files_include_committed_wal(tmp_path, role):
    """Native and adapter SQLite state must retain committed, uncheckpointed rows."""
    root, entries = _source(tmp_path)
    database = root / "native/profile.db"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE retained(value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('committed-in-wal')")
        connection.commit()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        entries.append(SnapshotEntry("native/profile.db", role, "sqlite"))
        snapshot = tmp_path / "snapshot"
        create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
        restored = tmp_path / "restored"
        restore_stopped_snapshot(snapshot, restored, runtime_stopped=True)
        with closing(sqlite3.connect(restored / "native/profile.db")) as retained:
            assert retained.execute("SELECT value FROM retained").fetchall() == [
                ("committed-in-wal",)
            ]
    finally:
        connection.close()


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_publication_preserves_destination_created_during_staging(
    tmp_path, monkeypatch, operation
):
    """A racing empty directory is still an existing installation to preserve."""
    from medre.maintenance import stopped_snapshot as subject

    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    if operation == "restore":
        create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    destination = snapshot if operation == "create" else tmp_path / "restored"
    make_stage = subject.tempfile.mkdtemp

    def competing_directory(*args, **kwargs):
        """Create a competing destination after the initial absence check but before publish."""
        stage = make_stage(*args, **kwargs)
        destination.mkdir()
        return stage

    monkeypatch.setattr(subject.tempfile, "mkdtemp", competing_directory)
    with pytest.raises(FileExistsError):
        if operation == "create":
            create_stopped_snapshot(root, destination, entries, runtime_stopped=True)
        else:
            restore_stopped_snapshot(snapshot, destination, runtime_stopped=True)
    assert destination.is_dir() and not list(destination.iterdir())


def test_manifest_rejects_duplicate_json_fields(tmp_path):
    """Duplicate fields must not silently replace the manifest's declared scope."""
    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    manifest = snapshot / "manifest.json"
    manifest.write_text(
        manifest.read_text().replace('"version":1', '"version":0,"version":1')
    )
    with pytest.raises(ValueError, match="duplicate"):
        verify_stopped_snapshot(snapshot)


def test_published_manifest_matches_its_schema(tmp_path):
    """The documented selected-state format matches the maintenance writer."""
    from jsonschema import Draft202012Validator

    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "docs/schemas/stopped-state-snapshot.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    data = json.loads((snapshot / "manifest.json").read_text())
    validator.validate(data)
    data["files"][0]["path"] = "../outside"
    assert not validator.is_valid(data)


def test_opaque_identity_bytes_are_not_text_translated(tmp_path):
    """Binary identity material survives Windows newline and EOF-byte semantics."""
    root, entries = _source(tmp_path)
    original = bytes(range(256))
    (root / "native/identity.key").write_bytes(original)
    snapshot, restored = tmp_path / "snapshot", tmp_path / "restored"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    restore_stopped_snapshot(snapshot, restored, runtime_stopped=True)
    assert (restored / "native/identity.key").read_bytes() == original


@pytest.mark.parametrize("field", ["role", "format"])
@pytest.mark.parametrize("value", [[], {}, None, True, "unsupported"])
def test_manifest_entry_types_share_constructor_validation(tmp_path, field, value):
    """Malformed declarations raise ValueError before selected files are accessed."""
    root, entries = _source(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_stopped_snapshot(root, snapshot, entries, runtime_stopped=True)
    manifest = snapshot / "manifest.json"
    data = json.loads(manifest.read_text())
    data["files"][0][field] = value
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="snapshot role|snapshot format"):
        verify_stopped_snapshot(snapshot)


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_opaque_capture_refuses_uncheckpointed_database_sidecars(tmp_path, suffix):
    """Encrypted or opaque native state cannot silently lose journaled writes."""
    root, entries = _source(tmp_path)
    key = root / "native/identity.key"
    key.with_name(key.name + suffix).write_bytes(b"uncheckpointed native state")
    with pytest.raises(ValueError, match="sidecar"):
        create_stopped_snapshot(
            root, tmp_path / "snapshot", entries, runtime_stopped=True
        )
    assert not (tmp_path / "snapshot").exists()


def test_opaque_declaration_cannot_bypass_sqlite_backup(tmp_path):
    """The role does not let a plaintext database opt out of its capture protocol."""
    root, entries = _source(tmp_path)
    # Keep exactly one core entry while selecting a separate SQLite copy.
    (root / "native/profile.db").write_bytes((root / "state/medre.sqlite").read_bytes())
    entries[1] = SnapshotEntry("native/profile.db", "native_identity", "opaque")
    with pytest.raises(ValueError, match="SQLite format"):
        create_stopped_snapshot(
            root, tmp_path / "snapshot", entries, runtime_stopped=True
        )


@pytest.mark.parametrize("operation", ["capture", "restore"])
@pytest.mark.parametrize("spelling", ["dot_segments", "ancestor_link"])
def test_output_cannot_be_nested_inside_its_source(tmp_path, operation, spelling):
    """Canonical containment applies to both snapshot capture and restoration."""
    root, entries = _source(tmp_path)
    if operation == "restore":
        source = tmp_path / "snapshot"
        create_stopped_snapshot(root, source, entries, runtime_stopped=True)
    else:
        source = root
    if spelling == "dot_segments":
        sibling = source.parent / "sibling"
        sibling.mkdir(exist_ok=True)
        destination = sibling / ".." / source.name / "output"
    else:
        alias = tmp_path / "source-alias"
        _symlink_or_skip(alias, source)
        destination = alias / "output"
    with pytest.raises(ValueError, match="inside the source"):
        if operation == "capture":
            create_stopped_snapshot(source, destination, entries, runtime_stopped=True)
        else:
            restore_stopped_snapshot(source, destination, runtime_stopped=True)
    assert not (source / "output").exists()


def test_symlink_fixture_does_not_hide_unrelated_setup_errors(tmp_path, monkeypatch):
    """Capability skips must not turn existing-path fixture defects into green tests."""

    def already_exists(*args, **kwargs):
        """Simulate a fixture collision rather than a missing platform capability."""
        raise FileExistsError(errno.EEXIST, "fixture collision")

    monkeypatch.setattr(Path, "symlink_to", already_exists)
    with pytest.raises(FileExistsError):
        _symlink_or_skip(tmp_path / "link", tmp_path / "target")
