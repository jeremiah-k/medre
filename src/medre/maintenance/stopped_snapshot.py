"""Stopped-runtime state snapshots with verified, all-or-nothing restoration.

The operator must stop MEDRE and its native workers before calling this API.
It intentionally does not discover secrets, read running profiles, modify an
existing installation, or treat a matching checksum as lifecycle authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, Sequence

SnapshotRole = Literal["core_sqlite", "adapter_ledger", "native_identity"]
SnapshotFormat = Literal["sqlite", "opaque"]
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotEntry:
    """Declare both semantic role and capture format; neither implies the other.

    Plain SQLite requires its backup protocol. Opaque provider exports and key
    files are byte copies, not an implicit fallback for unsupported databases.
    Construction and manifest parsing share this validation contract.
    """

    path: str
    role: SnapshotRole
    format: SnapshotFormat

    def __post_init__(self) -> None:
        """Reject malformed entries before capture or access to manifest files."""
        _relative(self.path)
        if not isinstance(self.role, str) or self.role not in {
            "core_sqlite",
            "adapter_ledger",
            "native_identity",
        }:
            raise ValueError("invalid snapshot role")
        if not isinstance(self.format, str) or self.format not in {"sqlite", "opaque"}:
            raise ValueError("invalid snapshot format")
        if self.role == "core_sqlite" and self.format != "sqlite":
            raise ValueError("core snapshot entry requires SQLite format")


@dataclass(frozen=True, slots=True)
class _VerifiedEntry:
    """Immutable bytes selected by verification, independent of later source reads."""

    entry: SnapshotEntry
    size: int
    sha256: str


def _open_regular(path: Path) -> BinaryIO:
    """Open a bounded regular input without following a replaced final symlink."""
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise ValueError("snapshot requires a bounded regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _copy_regular(source: Path, target: Path) -> None:
    """Copy through an owned descriptor; never create a destination symlink."""
    with _open_regular(source) as reader, target.open("xb") as writer:
        size = 0
        while block := reader.read(1024 * 1024):
            size += len(block)
            if size > _MAX_FILE_BYTES:
                raise ValueError("snapshot file exceeds the size bound")
            writer.write(block)


def _is_sqlite(path: Path) -> bool:
    """Detect an incorrectly declared opaque database without choosing its strategy."""
    with _open_regular(path) as source:
        return source.read(16) == b"SQLite format 3\x00"


def _check_opaque(path: Path) -> None:
    """Refuse opaque capture that could silently omit database journal state."""
    if _is_sqlite(path):
        raise ValueError("plaintext SQLite input requires explicit SQLite format")
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size > 0):
            raise ValueError(
                "opaque input has a database sidecar; use a qualified provider export"
            )


def _destination_outside(source: Path, destination: Path) -> Path:
    """Compare actual locations so dot segments or ancestor links cannot hide nesting."""
    resolved_source, resolved_destination = source.resolve(), destination.resolve()
    if (
        resolved_destination == resolved_source
        or resolved_source in resolved_destination.parents
    ):
        raise ValueError("snapshot destination must not be inside the source")
    return resolved_destination


def _publish(stage: Path, destination: Path) -> None:
    """Reserve an absent destination exclusively before replacing our empty marker.

    Readers can see an empty reservation before publication, but cannot verify a
    partial snapshot. A competing pre-existing directory is never replaced.
    """
    if os.name == "nt":
        # Windows rename refuses every existing destination atomically.
        os.rename(stage, destination)
        return
    destination.mkdir(mode=0o700)
    try:
        os.replace(stage, destination)
    except BaseException:
        destination.rmdir()
        raise


def _relative(value: str) -> Path:
    """Parse one canonical installation-relative path without alternate spellings."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or "\x00" in value
    ):
        raise ValueError("snapshot requires a safe relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("snapshot path must not escape the installation root")
    return Path(*path.parts)


def _regular(root: Path, relative: Path) -> Path:
    """Reject symlink components, special files, and absent inputs."""
    cursor = root
    if root.is_symlink():
        raise ValueError("installation root must not be a symlink")
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("snapshot must not follow symlinks")
    mode = cursor.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("snapshot requires regular files")
    if cursor.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("snapshot file exceeds the size bound")
    return cursor


def _digest(path: Path) -> tuple[int, str]:
    """Hash exact bytes without accepting files larger than the documented cap."""
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            if size > _MAX_FILE_BYTES:
                raise ValueError("snapshot file exceeds the size bound")
            digest.update(block)
    return size, digest.hexdigest()


def _check_sqlite(path: Path) -> None:
    """Validate the backed-up SQLite file without changing it."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("snapshot SQLite integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("snapshot SQLite foreign-key check failed")
    finally:
        conn.close()


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON fields rather than silently replacing verified claims."""
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate snapshot manifest field")
        values[key] = value
    return values


def create_stopped_snapshot(
    installation_root: Path,
    destination: Path,
    entries: Sequence[SnapshotEntry],
    *,
    runtime_stopped: bool = False,
) -> None:
    """Atomically publish a verified manifest and exact selected state files.

    The positive stopped-runtime assertion is mandatory but cannot prove that
    an external native worker has exited. No backup may be declared complete
    before the SQLite backup and all selected identity files are preserved.
    """
    if not runtime_stopped:
        raise ValueError("stop the runtime and native workers before snapshot")
    installation_root = Path(installation_root).absolute()
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("snapshot destination must not exist")
    destination = _destination_outside(installation_root, destination)
    if not entries or sum(item.role == "core_sqlite" for item in entries) != 1:
        raise ValueError("snapshot requires exactly one core SQLite entry")
    records: list[tuple[SnapshotEntry, Path]] = []
    used: set[str] = set()
    for entry in entries:
        relative = _relative(entry.path)
        if entry.path in used:
            raise ValueError("duplicate snapshot path")
        used.add(entry.path)
        records.append((entry, _regular(installation_root, relative)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".medre-snapshot-", dir=destination.parent))
    try:
        stored: list[dict[str, object]] = []
        for entry, source in sorted(records, key=lambda x: x[0].path):
            relative = _relative(entry.path)
            target = stage / "files" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.format == "sqlite":
                # SQLite's backup API includes committed WAL pages. An
                # exclusive writer lock prevents concurrent SQLite writers,
                # but native profile ownership still requires operator stop.
                guard = sqlite3.connect(str(source), timeout=0, isolation_level=None)
                try:
                    guard.execute("BEGIN IMMEDIATE")
                    reader = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
                    writer = sqlite3.connect(str(target))
                    try:
                        reader.backup(writer)
                        # The backup inherits WAL journal mode. Collapse its
                        # committed pages into one portable SQLite file.
                        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        writer.execute("PRAGMA journal_mode=DELETE")
                    finally:
                        writer.close()
                        reader.close()
                    guard.execute("ROLLBACK")
                finally:
                    guard.close()
                _check_sqlite(target)
            else:
                _check_opaque(source)
                _copy_regular(source, target)
            os.chmod(target, 0o600)
            size, sha256 = _digest(target)
            stored.append(
                {
                    "path": entry.path,
                    "role": entry.role,
                    "format": entry.format,
                    "bytes": size,
                    "sha256": sha256,
                }
            )
        manifest = {"format": "medre-stopped-state", "version": 1, "files": stored}
        (stage / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(stage / "manifest.json", 0o600)
        verify_stopped_snapshot(stage)
        _publish(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _verified_files(snapshot: Path) -> tuple[_VerifiedEntry, ...]:
    """Retain verified manifest digests rather than trusting later source contents."""
    snapshot = Path(snapshot).absolute()
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("snapshot must be a directory without symlinks")
    manifest_path = _regular(snapshot, Path("manifest.json"))
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("snapshot manifest is too large")
    with _open_regular(manifest_path) as source:
        raw = source.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError("snapshot manifest is too large")
    data = json.loads(raw, object_pairs_hook=_unique_keys)
    if (
        not isinstance(data, dict)
        or set(data) != {"format", "version", "files"}
        or data["format"] != "medre-stopped-state"
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["files"], list)
    ):
        raise ValueError("unsupported stopped snapshot manifest")
    files: list[_VerifiedEntry] = []
    expected = {"manifest.json"}
    for row in data["files"]:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "role",
            "format",
            "bytes",
            "sha256",
        }:
            raise ValueError("invalid snapshot manifest entry")
        entry = SnapshotEntry(row["path"], row["role"], row["format"])
        relative = _relative(entry.path)
        target = _regular(snapshot, Path("files") / relative)
        size, sha = _digest(target)
        if (
            type(row["bytes"]) is not int
            or row["bytes"] != size
            or row["sha256"] != sha
        ):
            raise ValueError("snapshot file checksum mismatch")
        name = (Path("files") / relative).as_posix()
        if name in expected:
            raise ValueError("duplicate snapshot manifest entry")
        expected.add(name)
        if entry.format == "sqlite":
            _check_sqlite(target)
        else:
            _check_opaque(target)
        files.append(_VerifiedEntry(entry, size, sha))
    if sum(record.entry.role == "core_sqlite" for record in files) != 1:
        raise ValueError("snapshot must contain exactly one core database")
    actual = {
        p.relative_to(snapshot).as_posix()
        for p in snapshot.rglob("*")
        if not p.is_dir()
    }
    if actual != expected or any(p.is_symlink() for p in snapshot.rglob("*")):
        raise ValueError("snapshot contains missing or unexpected files")
    return tuple(files)


def verify_stopped_snapshot(snapshot: Path) -> tuple[SnapshotEntry, ...]:
    """Fail closed for corrupt, missing, unexpected, or symlink-backed content."""
    return tuple(record.entry for record in _verified_files(snapshot))


def restore_stopped_snapshot(
    snapshot: Path, destination_root: Path, *, runtime_stopped: bool = False
) -> None:
    """Restore only into a new installation directory; never overwrite a live one."""
    if not runtime_stopped:
        raise ValueError("stop the runtime and native workers before restore")
    destination_root = Path(destination_root).absolute()
    if destination_root.exists() or destination_root.is_symlink():
        raise FileExistsError("restore requires an absent destination directory")
    destination_root = _destination_outside(Path(snapshot), destination_root)
    files = _verified_files(snapshot)
    destination_root.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=".medre-restore-", dir=destination_root.parent)
    )
    try:
        for record in files:
            entry = record.entry
            source = Path(snapshot).absolute() / "files" / _relative(entry.path)
            target = stage / _relative(entry.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_regular(source, target)
            os.chmod(target, 0o600)
            if _digest(target) != (record.size, record.sha256):
                raise ValueError("snapshot changed during restore")
        _publish(stage, destination_root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
