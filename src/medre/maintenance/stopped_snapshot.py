"""Stopped-runtime state snapshots with verified, all-or-nothing restoration.

The operator must stop MEDRE and its native workers before calling this API.
It intentionally does not discover secrets, read running profiles, modify an
existing installation, or treat a matching checksum as lifecycle authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
_NATIVE_COLONS = os.name != "nt"

STOPPED_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "runtime_not_stopped",
        "destination_exists",
        "destination_inside_source",
        "snapshot_not_directory",
        "manifest_too_large",
        "duplicate_manifest_field",
        "unsupported_manifest",
        "invalid_manifest_entry",
        "duplicate_manifest_entry",
        "core_entry_count",
        "invalid_snapshot_role",
        "invalid_snapshot_format",
        "core_entry_requires_sqlite",
        "unsafe_relative_path",
        "path_escapes_root",
        "root_is_symlink",
        "symlink_in_path",
        "not_regular_file",
        "file_exceeds_size_bound",
        "checksum_mismatch",
        "sqlite_integrity_failed",
        "sqlite_foreign_key_failed",
        "opaque_is_sqlite",
        "opaque_has_sidecar",
        "missing_or_unexpected_files",
        "snapshot_changed_during_restore",
        "watermark_unreadable",
        "newer_live_evidence",
        "divergent_watermarks",
        "live_evidence_unreadable",
        "live_state_diverged",
        "fingerprint_unreadable",
    }
)


def _checked_reason(reason: str) -> str:
    """Keep every refusal reason inside the closed vocabulary."""
    if reason not in STOPPED_REFUSAL_REASONS:
        raise ValueError("refusal reason is outside the closed vocabulary")
    return reason


class StoppedStateRefused(ValueError):
    """Validation refusal that carries a content-free reason code.

    The message text keeps its existing library wording; ``reason`` is a
    fixed identifier from ``STOPPED_REFUSAL_REASONS`` and never carries a
    path, file name, identifier, timestamp, size, count, or OS text.
    """

    reason: str

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = _checked_reason(reason)


class StoppedStateDestinationRefused(FileExistsError):
    """Absent-destination refusal that carries a reason code.

    Subclasses :class:`FileExistsError` so callers that depend on that
    type keep working while the refusal still names a reason.
    """

    reason: str

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = _checked_reason(reason)


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
            raise StoppedStateRefused(
                "invalid snapshot role", reason="invalid_snapshot_role"
            )
        if not isinstance(self.format, str) or self.format not in {"sqlite", "opaque"}:
            raise StoppedStateRefused(
                "invalid snapshot format", reason="invalid_snapshot_format"
            )
        if self.role == "core_sqlite" and self.format != "sqlite":
            raise StoppedStateRefused(
                "core snapshot entry requires SQLite format",
                reason="core_entry_requires_sqlite",
            )


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
        if not stat.S_ISREG(info.st_mode):
            raise StoppedStateRefused(
                "snapshot requires a bounded regular file", reason="not_regular_file"
            )
        if info.st_size > _MAX_FILE_BYTES:
            raise StoppedStateRefused(
                "snapshot requires a bounded regular file",
                reason="file_exceeds_size_bound",
            )
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
                raise StoppedStateRefused(
                    "snapshot file exceeds the size bound",
                    reason="file_exceeds_size_bound",
                )
            writer.write(block)


def _is_sqlite(path: Path) -> bool:
    """Detect an incorrectly declared opaque database without choosing its strategy."""
    with _open_regular(path) as source:
        return source.read(16) == b"SQLite format 3\x00"


def _check_opaque(path: Path) -> None:
    """Refuse opaque capture that could silently omit database journal state."""
    if _is_sqlite(path):
        raise StoppedStateRefused(
            "plaintext SQLite input requires explicit SQLite format",
            reason="opaque_is_sqlite",
        )
    for suffix in ("-wal", "-journal"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and sidecar.stat().st_size > 0):
            raise StoppedStateRefused(
                "opaque input has a database sidecar; use a qualified provider export",
                reason="opaque_has_sidecar",
            )


def _destination_outside(source: Path, destination: Path) -> Path:
    """Compare actual locations so dot segments or ancestor links cannot hide nesting."""
    resolved_source, resolved_destination = source.resolve(), destination.resolve()
    if (
        resolved_destination == resolved_source
        or resolved_source in resolved_destination.parents
    ):
        raise StoppedStateRefused(
            "snapshot destination must not be inside the source",
            reason="destination_inside_source",
        )
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
    """Parse a host-representable relative path without drive or stream aliases.

    POSIX SDK filenames may contain a colon (for example Matrix account IDs).
    Such snapshots are host-specific: Windows rejects them before any file is
    opened, since a colon can select an alternate data stream there. Drive
    spellings, backslashes and traversal are rejected on every host.
    """
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or (":" in value and not _NATIVE_COLONS)
        or re.search(r"(?:^|/)[A-Za-z]:", value) is not None
        or "\x00" in value
    ):
        raise StoppedStateRefused(
            "snapshot requires a safe relative POSIX path",
            reason="unsafe_relative_path",
        )
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise StoppedStateRefused(
            "snapshot path must not escape the installation root",
            reason="path_escapes_root",
        )
    return Path(*path.parts)


def _regular(root: Path, relative: Path) -> Path:
    """Reject symlink components, special files, and absent inputs."""
    cursor = root
    if root.is_symlink():
        raise StoppedStateRefused(
            "installation root must not be a symlink", reason="root_is_symlink"
        )
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise StoppedStateRefused(
                "snapshot must not follow symlinks", reason="symlink_in_path"
            )
    mode = cursor.stat().st_mode
    if not stat.S_ISREG(mode):
        raise StoppedStateRefused(
            "snapshot requires regular files", reason="not_regular_file"
        )
    if cursor.stat().st_size > _MAX_FILE_BYTES:
        raise StoppedStateRefused(
            "snapshot file exceeds the size bound", reason="file_exceeds_size_bound"
        )
    return cursor


def _digest(path: Path) -> tuple[int, str]:
    """Hash exact bytes without accepting files larger than the documented cap."""
    digest = hashlib.sha256()
    size = 0
    with _open_regular(path) as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            size += len(block)
            if size > _MAX_FILE_BYTES:
                raise StoppedStateRefused(
                    "snapshot file exceeds the size bound",
                    reason="file_exceeds_size_bound",
                )
            digest.update(block)
    return size, digest.hexdigest()


def _check_sqlite(path: Path) -> None:
    """Validate the backed-up SQLite file without changing it."""
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise StoppedStateRefused(
                "snapshot SQLite integrity check failed",
                reason="sqlite_integrity_failed",
            )
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise StoppedStateRefused(
                "snapshot SQLite foreign-key check failed",
                reason="sqlite_foreign_key_failed",
            )
    finally:
        conn.close()


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON fields rather than silently replacing verified claims."""
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise StoppedStateRefused(
                "duplicate snapshot manifest field", reason="duplicate_manifest_field"
            )
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
        raise StoppedStateRefused(
            "snapshot must be a directory without symlinks",
            reason="snapshot_not_directory",
        )
    manifest_path = _regular(snapshot, Path("manifest.json"))
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise StoppedStateRefused(
            "snapshot manifest is too large", reason="manifest_too_large"
        )
    with _open_regular(manifest_path) as source:
        raw = source.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise StoppedStateRefused(
            "snapshot manifest is too large", reason="manifest_too_large"
        )
    data = json.loads(raw, object_pairs_hook=_unique_keys)
    if (
        not isinstance(data, dict)
        or set(data) != {"format", "version", "files"}
        or data["format"] != "medre-stopped-state"
        or type(data["version"]) is not int
        or data["version"] != 1
        or not isinstance(data["files"], list)
    ):
        raise StoppedStateRefused(
            "unsupported stopped snapshot manifest", reason="unsupported_manifest"
        )
    # Validate the complete selection before opening any selected file. A
    # later host-incompatible path cannot permit earlier entries to be read.
    declared: list[tuple[SnapshotEntry, dict[str, object]]] = []
    expected = {"manifest.json"}
    for row in data["files"]:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "role",
            "format",
            "bytes",
            "sha256",
        }:
            raise StoppedStateRefused(
                "invalid snapshot manifest entry", reason="invalid_manifest_entry"
            )
        entry = SnapshotEntry(row["path"], row["role"], row["format"])
        relative = _relative(entry.path)
        name = (Path("files") / relative).as_posix()
        if name in expected:
            raise StoppedStateRefused(
                "duplicate snapshot manifest entry", reason="duplicate_manifest_entry"
            )
        expected.add(name)
        declared.append((entry, row))
    if sum(entry.role == "core_sqlite" for entry, _ in declared) != 1:
        raise StoppedStateRefused(
            "snapshot must contain exactly one core database", reason="core_entry_count"
        )
    files: list[_VerifiedEntry] = []
    for entry, row in declared:
        target = _regular(snapshot, Path("files") / _relative(entry.path))
        size, sha = _digest(target)
        if (
            type(row["bytes"]) is not int
            or row["bytes"] != size
            or row["sha256"] != sha
        ):
            raise StoppedStateRefused(
                "snapshot file checksum mismatch", reason="checksum_mismatch"
            )
        if entry.format == "sqlite":
            _check_sqlite(target)
        else:
            _check_opaque(target)
        files.append(_VerifiedEntry(entry, size, sha))
    actual = {
        p.relative_to(snapshot).as_posix()
        for p in snapshot.rglob("*")
        if not p.is_dir()
    }
    if actual != expected or any(p.is_symlink() for p in snapshot.rglob("*")):
        raise StoppedStateRefused(
            "snapshot contains missing or unexpected files",
            reason="missing_or_unexpected_files",
        )
    return tuple(files)


def verify_stopped_snapshot(snapshot: Path) -> tuple[SnapshotEntry, ...]:
    """Fail closed for corrupt, missing, unexpected, or symlink-backed content."""
    return tuple(record.entry for record in _verified_files(snapshot))


def read_delivery_evidence_watermark(db_path: Path) -> int:
    """Return the content-free, monotone delivery-evidence watermark.

    The watermark is the ``sqlite_sequence`` high-water of
    ``delivery_receipts.sequence`` — the identity family behind the
    receipt-less handoff settlement fence, which freezes the receipt
    watermark at every terminal delivery transition. ``sequence`` is an
    ``INTEGER PRIMARY KEY AUTOINCREMENT`` identity, and the ``sqlite_sequence``
    row is the largest identity ever allocated: it grows monotonically and
    is never lowered by deleting rows. That matters because receipts are
    themselves purgeable at the content ceiling — a plain ``MAX(sequence)``
    could decay after a purge and make a genuinely stale backup look
    acceptable again. The value carries no message content, addresses, or
    payloads. Wall-clock timestamps were deliberately rejected: clock skew
    can make a stale backup look newer than live state.

    A missing database file has watermark ``0`` (nothing to protect). A
    readable database without a ``sqlite_sequence`` row for
    ``delivery_receipts`` has watermark ``0`` (no delivery identities ever
    allocated). A database that exists but cannot answer the read raises
    :class:`ValueError` so callers fail closed instead of guessing.
    """
    resolved = Path(db_path).absolute()
    if not resolved.exists():
        return 0
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='delivery_receipts'"
        ).fetchone()
        return int(row[0]) if row is not None else 0
    except sqlite3.Error as exc:
        raise StoppedStateRefused(
            "delivery-evidence watermark is unreadable for this database; "
            "refusing to compare it",
            reason="watermark_unreadable",
        ) from exc
    finally:
        conn.close()


_FINGERPRINT_MODULUS = 1 << 256
_ROW_BATCH = 512
_MAX_SUMMARY_NAMES = 8
_STAT_TABLE_PREFIX = "sqlite_stat"


def _feed_bytes(digest: hashlib._Hash, value: bytes) -> None:
    """Mix length-prefixed bytes so concatenations cannot alias."""
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _feed_text(digest: hashlib._Hash, value: str) -> None:
    _feed_bytes(digest, value.encode("utf-8", errors="surrogatepass"))


def _feed_value(digest: hashlib._Hash, value: object) -> None:
    """Encode one SQLite value with a type tag so distinct values never alias."""
    if value is None:
        _feed_text(digest, "null")
    elif isinstance(value, int):
        _feed_text(digest, f"int:{value}")
    elif isinstance(value, float):
        _feed_text(digest, f"float:{value!r}")
    elif isinstance(value, str):
        _feed_text(digest, f"text:{value}")
    elif isinstance(value, bytes):
        _feed_bytes(digest, b"blob:" + value)
    else:
        raise StoppedStateRefused(
            "delivery evidence cannot be compared with the existing database",
            reason="fingerprint_unreadable",
        )


def _quoted_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_rows_fingerprint(conn: sqlite3.Connection, table: str) -> tuple[int, int]:
    """Stream every row and return (row count, order-independent content sum).

    Rows arrive in physical order, which ``VACUUM`` may change for tables
    without an integer rowid alias. Summing each row's digest makes the
    accumulator order-independent while still counting duplicates.
    """
    rows = 0
    total = 0
    cursor = conn.execute(
        f"SELECT * FROM {_quoted_identifier(table)}"  # nosec B608 - identifier is doubled-quote escaped and comes only from the compared database's own catalog
    )
    while batch := cursor.fetchmany(_ROW_BATCH):
        for row in batch:
            row_digest = hashlib.sha256()
            for value in row:
                _feed_value(row_digest, value)
            total = (
                total + int.from_bytes(row_digest.digest(), "big")
            ) % _FINGERPRINT_MODULUS
            rows += 1
    return rows, total


@dataclass(frozen=True, slots=True)
class _LogicalTableFingerprint:
    rows: int
    content: int


@dataclass(frozen=True, slots=True)
class _LogicalDatabaseFingerprint:
    digest: str
    tables: dict[str, _LogicalTableFingerprint]


def _logical_database_fingerprint(
    db_path: Path,
) -> _LogicalDatabaseFingerprint:
    """Hash the stopped database's logical catalog and row multisets.

    A receipt sequence is not a lineage proof: different branches can allocate
    the same identity, and operator decisions need not allocate any receipt.
    Compare full logical state when an existing installation is supplied.
    Physical operations that preserve logical state must not change this
    fingerprint: ``VACUUM`` rebuilds ``sqlite_master`` in a different order and
    renumbers rowids of tables without an integer alias, and ``ANALYZE`` adds
    planner-statistics tables. The catalog is therefore hashed in sorted order,
    each table's rows are hashed as an order-independent multiset, and the
    ``sqlite_stat`` planner tables are skipped. The internal ``sqlite_sequence``
    counters are included because receipt sequences are evidence. This check is
    intentionally conservative, streams row batches without materialising the
    dump, and never exposes row values outside the hash.
    """
    try:
        conn = sqlite3.connect(f"{db_path.absolute().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("BEGIN")
            digest = hashlib.sha256()
            catalog = conn.execute(
                "SELECT type, name, sql FROM sqlite_master"
                " WHERE sql NOT NULL ORDER BY type, name"
            ).fetchall()
            for kind, name, sql in catalog:
                if str(name).startswith(_STAT_TABLE_PREFIX):
                    continue
                _feed_text(digest, str(kind))
                _feed_text(digest, str(name))
                _feed_text(digest, str(sql))
            table_names = sorted(
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            tables: dict[str, _LogicalTableFingerprint] = {}
            for name in table_names:
                if name.startswith(_STAT_TABLE_PREFIX):
                    continue
                _feed_text(digest, name)
                rows, content = _table_rows_fingerprint(conn, name)
                digest.update(rows.to_bytes(8, "big"))
                digest.update(content.to_bytes(32, "big"))
                tables[name] = _LogicalTableFingerprint(rows, content)
            conn.rollback()
            return _LogicalDatabaseFingerprint(digest.hexdigest(), tables)
        finally:
            conn.close()
    except (sqlite3.Error, UnicodeError, OverflowError) as exc:
        raise StoppedStateRefused(
            "delivery evidence cannot be compared with the existing database",
            reason="fingerprint_unreadable",
        ) from exc


def _bounded_name_list(names: list[str]) -> str:
    if len(names) <= _MAX_SUMMARY_NAMES:
        return ", ".join(names)
    kept = ", ".join(names[:_MAX_SUMMARY_NAMES])
    return f"{kept}, and {len(names) - _MAX_SUMMARY_NAMES} more"


def _divergence_summary(
    staged: _LogicalDatabaseFingerprint, live: _LogicalDatabaseFingerprint
) -> str:
    """Describe which tables differ, by name and row count only — never values."""
    names = sorted(set(staged.tables) | set(live.tables))
    only_staged = [name for name in names if name not in live.tables]
    only_live = [name for name in names if name not in staged.tables]
    row_counts: list[str] = []
    contents: list[str] = []
    for name in names:
        staged_table = staged.tables.get(name)
        live_table = live.tables.get(name)
        if staged_table is None or live_table is None:
            continue
        if staged_table.rows != live_table.rows:
            row_counts.append(
                f"{name} (staged rows {staged_table.rows},"
                f" live rows {live_table.rows})"
            )
        elif staged_table.content != live_table.content:
            contents.append(name)
    parts: list[str] = []
    if only_staged:
        parts.append(
            "tables present only in the staged database: "
            + _bounded_name_list(only_staged)
        )
    if only_live:
        parts.append(
            "tables present only in the live database: " + _bounded_name_list(only_live)
        )
    if row_counts:
        parts.append("row counts differ for: " + _bounded_name_list(row_counts))
    if contents:
        parts.append(
            "equal row counts but different rows for: " + _bounded_name_list(contents)
        )
    return "; ".join(parts) if parts else "logical database content differs"


def _assert_backup_not_stale(staged_database: Path, live: Path) -> None:
    """Require identical logical state before replacing a known installation.

    A receipt high-water detects a strictly older branch, but equality alone
    cannot distinguish independently appended receipts or operator decisions,
    so equal watermarks additionally require equal logical fingerprints. The
    caller compares the *verified staged* database, never a source file that
    could change between inspection and publication. Physical-only maintenance
    on the live database (``VACUUM``, ``ANALYZE``) still restores. Other
    native files and provider-side effects still require operator
    reconciliation.
    """
    backup_watermark = read_delivery_evidence_watermark(staged_database)
    try:
        live_watermark = read_delivery_evidence_watermark(live)
    except ValueError as exc:
        raise StoppedStateRefused(
            "the live database holds unreadable delivery evidence; refusing "
            "to restore a backup over it",
            reason="live_evidence_unreadable",
        ) from exc
    if backup_watermark < live_watermark:
        raise StoppedStateRefused(
            "refusing to restore a stale backup: the backup's delivery-evidence "
            f"watermark is {backup_watermark} while the live database's is "
            f"{live_watermark}; restoring would erase newer delivery evidence. "
            "Capture a fresh snapshot of the live installation, or restore to "
            "an unused destination and reconcile the evidence explicitly.",
            reason="newer_live_evidence",
        )
    if backup_watermark != live_watermark:
        raise StoppedStateRefused(
            "refusing to restore a divergent backup: receipt watermarks do not "
            "prove shared history or preserve later operator decisions. "
            "Capture a fresh snapshot or reconcile the branches offline.",
            reason="divergent_watermarks",
        )
    staged_fingerprint = _logical_database_fingerprint(staged_database)
    live_fingerprint = _logical_database_fingerprint(live)
    if staged_fingerprint.digest != live_fingerprint.digest:
        raise StoppedStateRefused(
            "refusing to restore a divergent backup: the staged database's "
            "logical state differs from the live database's "
            f"({_divergence_summary(staged_fingerprint, live_fingerprint)}). "
            "Receipt watermarks do not prove shared history or preserve later "
            "operator decisions. Capture a fresh snapshot or reconcile the "
            "branches offline.",
            reason="live_state_diverged",
        )


def restore_stopped_snapshot(
    snapshot: Path,
    destination_root: Path,
    *,
    runtime_stopped: bool = False,
    live_database: Path | None = None,
) -> None:
    """Restore only into a new installation directory; never overwrite a live one.

    When *live_database* names an existing SQLite database, compare the
    verified staged core database against the stopped live database's full
    logical state. The comparison rejects diverged branches even if receipt
    sequence watermarks match. A missing live path skips this optional guard;
    the operator must reconcile native effects and decisions independently.
    """
    if not runtime_stopped:
        raise StoppedStateRefused(
            "stop the runtime and native workers before restore",
            reason="runtime_not_stopped",
        )
    destination_root = Path(destination_root).absolute()
    if destination_root.exists() or destination_root.is_symlink():
        raise StoppedStateDestinationRefused(
            "restore requires an absent destination directory",
            reason="destination_exists",
        )
    destination_root = _destination_outside(Path(snapshot), destination_root)
    files = _verified_files(snapshot)
    core_entry = (
        next(record.entry for record in files if record.entry.role == "core_sqlite")
        if live_database is not None and Path(live_database).exists()
        else None
    )
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
                raise StoppedStateRefused(
                    "snapshot changed during restore",
                    reason="snapshot_changed_during_restore",
                )
        if core_entry is not None and live_database is not None:
            _assert_backup_not_stale(
                stage / _relative(core_entry.path), Path(live_database).absolute()
            )
        _publish(stage, destination_root)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
