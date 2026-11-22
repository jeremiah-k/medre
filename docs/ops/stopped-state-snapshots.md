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

Restoring into a new directory does not automatically compare an existing
installation. When the restored directory may replace an existing installation,
pass its stopped core database with `--against-live PATH` (or `live_database`).
That option compares the **verified staged database** against the stopped live
core database. Any receipt high-water difference refuses the restore in either
direction — a lower one would erase newer delivery evidence, and a higher one
marks the branches as divergent — and a different logical catalog or row
history is refused **even when the high-water numbers match**: operator
decisions and native effects need not append receipts, and different branches
can allocate the same sequence. Purely physical maintenance on the live
database (`VACUUM`, `ANALYZE`) does not refuse. An unreadable live database
also refuses the restore. A `--against-live` path that is omitted or does not
exist skips this comparison and MUST NOT be treated as evidence that a
replacement is safe. Never start both installations together. Reconcile
provider-side effects separately, since a matching core database does not
attest native systems. See
[operator workflows](operator-workflows.md) for the full policy wording.

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

## Explicit inventory and maintenance command

A stopped installation can use the separate administrative CLI without starting
MEDRE or any adapter. Prepare a JSON inventory under administrator control:
its structure is described in the
[inventory schema](../schemas/stopped-state-inventory.schema.json).

```json
{
  "format": "medre-stopped-inventory",
  "version": 1,
  "providers": ["core", "simplexchat"],
  "files": [
    {
      "provider": "core",
      "path": "state/medre.sqlite",
      "role": "core_sqlite",
      "format": "sqlite"
    },
    {
      "provider": "simplexchat",
      "path": "adapters/simplexchat/handoffs.sqlite",
      "role": "adapter_ledger",
      "format": "sqlite"
    },
    {
      "provider": "simplexchat",
      "path": "adapters/simplexchat/inbox.sqlite",
      "role": "adapter_ledger",
      "format": "sqlite"
    },
    {
      "provider": "simplexchat",
      "path": "adapters/simplexchat/profile.medre-owner.sqlite",
      "role": "adapter_ledger",
      "format": "sqlite"
    },
    {
      "provider": "simplexchat",
      "path": "adapters/simplexchat/profile_chat.db",
      "role": "native_identity",
      "format": "sqlite"
    }
  ]
}
```

The paths above are **illustrative**, not automatically discovered native SDK
filenames. In particular, confirm the actual native SimpleX profile database
names from the installed SDK and the configured `database_prefix`; include
**every** profile database and identity-bearing file. Include the
`profile.medre-owner.lock` Linux ownership guard as an opaque identity file
when present; do not rely on its saved bytes to establish a live lock after
restore. Map configured `state_dir`, `native_cache_dir` (`XDG_CACHE_HOME`),
`database_prefix`, and any other identity-bearing XDG directories into the
selected common root. If configured state lives outside that root, stop the
native SDK and prepare a coherent provider-owned export before capture; do
not point a manifest through symlinks or claim that a copied partial tree is
complete. An inventory's declared provider set is closed and each listed
provider must own selected state, but this cannot detect unlisted native
state. The operator must compare the inventory to actual provider locations.

```bash
python -m medre.maintenance.stopped_cli capture \
  --inventory inventory.json --root /srv/medre-state \
  --snapshot /secure-backups/medre-snapshot-001 --stopped
python -m medre.maintenance.stopped_cli verify \
  --snapshot /secure-backups/medre-snapshot-001
python -m medre.maintenance.stopped_cli restore \
  --snapshot /secure-backups/medre-snapshot-001 \
  --destination /srv/medre-restored \
  --against-live /srv/medre-state/state/medre.sqlite --stopped
```

`--stopped` is deliberately required for capture and restore; it supplies an
operator assertion, **not** an automatic check of subprocess or SDK state.
The destination must not exist. Preserve the snapshot securely: it can contain
unencrypted profile secrets. Successful and operational outcomes emit only
status, never file names, key content, or private absolute paths.

A refused `restore` prints one JSON line carrying the operation, the status,
the error type and, for a refusal MEDRE raises itself, a fixed `reason` code:

```json
{
  "operation": "restore",
  "status": "refused",
  "error_type": "StoppedStateRefused",
  "reason": "newer_live_evidence"
}
```

The codes form a closed vocabulary, `STOPPED_REFUSAL_REASONS` in
`medre.maintenance.stopped_snapshot`. Each names one kind of refusal and
never carries a file name, identifier, count, timestamp, size, or path:

| Refusal                       | Reason codes                                                                                                                                                                                                                     |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runtime and destination       | `runtime_not_stopped`, `destination_exists`, `destination_inside_source`                                                                                                                                                         |
| Manifest and file set         | `snapshot_not_directory`, `manifest_too_large`, `duplicate_manifest_field`, `unsupported_manifest`, `invalid_manifest_entry`, `duplicate_manifest_entry`, `core_entry_count`, `checksum_mismatch`, `missing_or_unexpected_files` |
| Declared entry, path and file | `invalid_snapshot_role`, `invalid_snapshot_format`, `core_entry_requires_sqlite`, `unsafe_relative_path`, `path_escapes_root`, `root_is_symlink`, `symlink_in_path`, `not_regular_file`, `file_exceeds_size_bound`               |
| Database files                | `sqlite_integrity_failed`, `sqlite_foreign_key_failed`, `opaque_is_sqlite`, `opaque_has_sidecar`, `watermark_unreadable`, `fingerprint_unreadable`                                                                               |
| Restore-time checks           | `snapshot_changed_during_restore`, `newer_live_evidence`, `divergent_watermarks`, `live_evidence_unreadable`, `live_state_diverged`                                                                                              |

`capture` and `verify` report the same codes for the checks they share with
`restore`. Every other refusal reports the error type only: an operating-system
error, a manifest or inventory the JSON parser rejects, a configuration failure,
or a check that applies to `capture` alone. Invalid CLI syntax emits a generic
argument error without echoing supplied path values.

Before starting a restored installation, independently verify the provider
profiles and ensure the original installation is not simultaneously active. Unknown radio work remains
held and is not replay-authorized by restoring its database.

Provider labels validate the capture selection; they are not stored as a
provider-completeness certificate in the snapshot. Keep the inventory alongside
the installation's configuration and provider instructions. `verify` checks the
captured manifest and files; it cannot detect state omitted from the inventory.

Instead of hand-writing the file list, `medre.maintenance.stopped_declare` can
derive it from per-provider declarations: each configured provider resolves its
own identity, journal, and lock files, and anything that cannot honestly be
captured by files (device firmware, encrypted profiles, credentials outside
the root) is reported as export-only instead of silently omitted. See
[provider stopped-state declarations](stopped-state-provider-inventory.md)
for the derivation command, the supported per-provider scope, the point-in-time
`check` command, and the library `retain_local_exclusion` helper. The library
helper retains local exclusion across capture only while its context is held;
separate CLI invocations do not retain locks between commands.

The [native evidence index](../dev/native-evidence-index.json) records a stopped
core/SimpleX restore using seven selected files, including committed core WAL
content, native profile databases and adapter ledgers. The relocated native
profile reopened and reused an accepted handoff without another submission.
That result covers the recorded installation and SDK revision; each other
provider requires its actual identity and journal inventory.
