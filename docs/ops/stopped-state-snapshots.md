# Coherent stopped-installation snapshots

`medre.maintenance.stopped_snapshot` provides an opt-in maintenance API for a
**stopped** single-host installation. It is not a live backup tool and cannot
stop the runtime, close SDK profiles, attest native process death, or decide
whether retained radio packets may be retransmitted. The caller must stop
MEDRE and every native profile owner first, and must enumerate every state file
needed to restore that particular installation.

The operator supplies an installation root and explicit `SnapshotEntry` paths:
one `core_sqlite`, plus the relevant `adapter_ledger` and `native_identity`
files. Each entry also requires an explicit `format`: `sqlite` for a plaintext
SQLite database or `opaque` for a self-contained provider export or key file.
For example, `SnapshotEntry("state/medre.sqlite", "core_sqlite", "sqlite")`
selects the database backup protocol. Role labels never choose a capture format.
Omission is not detected by SDK introspection. Treat secret native
identity material as sensitive; the snapshot is not encrypted. Store it on a
restricted volume and transfer it only through trusted channels.

`create_stopped_snapshot(root, output, entries, runtime_stopped=True)` uses
SQLite's backup API for every selected plaintext SQLite file, including adapter
ledgers and native profiles, to retain committed WAL content in each standalone
database. It includes named opaque native files, hashes all selected bytes, and publishes a
complete directory only after verification. Source symlinks, noncanonical
paths, duplicate names, invalid database integrity, and existing destinations
are rejected. The stopped-runtime flag is an explicit operator assertion,
**not proof** that a native process is absent.

`verify_stopped_snapshot(output)` rejects missing, altered, unexpected or
symlinked content. `restore_stopped_snapshot(output, new_root,
 runtime_stopped=True)` verifies first and restores atomically into an
**absent destination directory**. Staged copies must match the original verified digests even if the snapshot
changes during restore. A destination is exclusively reserved before publication;
a temporary empty reservation is not a valid snapshot. Existing directories,
including ones created during staging, are preserved.
Use a separate administrative switch of the installation root only after
validating native identity/profiles and confirming all old owners are stopped.

No delivery attempt is sent, reopened, or changed by these functions. A
successful file checksum proves byte integrity, not the safety of replaying an
unknown native send. Recovery must continue to use the existing outbox claim
and packet evidence checks after activation. Production installations still
need provider-specific file inventories and independently tested restore runs.

An opaque input with a nonempty `-wal` or `-journal` sidecar is refused. Plaintext
SQLite cannot be labelled opaque to bypass backup. Encrypted databases require a
provider-produced consistent export before this generic library can capture it.
Output locations are resolved and must stay outside the capture or restore source,
including paths containing `..` or symlinked ancestors.
This library does not claim durable publication across power or filesystem failure.
