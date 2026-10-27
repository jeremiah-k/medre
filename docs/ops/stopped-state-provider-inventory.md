# Provider stopped-state declarations

The [stopped-state snapshot](stopped-state-snapshots.md) procedure needs an
exact inventory of the files that make up one stopped installation. Writing
that inventory by hand is error-prone: each provider keeps identity, journals,
and lock files in its own layout, and a missing file silently becomes a
restored installation that cannot authenticate or that replays work twice.
`medre.maintenance.stopped_declare` derives the inventory from per-provider
declarations instead. The result is still an operator-confirmed selection, not
a provider-completeness certificate — but every provider that cannot be
described by files is labeled honestly instead of silently omitted.

## Deriving an inventory from the configured installation

With MEDRE stopped, run:

```bash
python -m medre.maintenance.stopped_cli plan \
  --config /etc/medre/config.yaml \
  --root /srv/medre-state \
  --inventory /secure-backups/inventory.json
python -m medre.maintenance.stopped_cli check \
  --config /etc/medre/config.yaml \
  --root /srv/medre-state
```

`plan` loads the configuration exactly as the runtime would (including
`MEDRE_*` environment overrides), asks each configured provider to resolve its
own paths, and writes a version-1 inventory. Each adapter distribution ships
its own declaration module; core resolves it through the adapter spec and
keeps only the core database declaration in core. Every selected file must
live inside the common state root; two providers claiming the same file, a
provider whose installed distribution ships no declaration, or required state
outside the root are refused. `check` resolves the same closed selection as
`plan`, so a configured distribution without a usable declaration is refused
as unsupported before any ownership primitive is touched. It then acquires
each file-backed provider's own ownership primitive — the flock guard or
lease database a running worker would hold — and releases it when the command
exits. A primitive refusal means a local owner is still active or the
primitive has never been created; it never means another host is inactive.
Strict checking (the default) refuses the command when a file-backed provider
has no implemented primitive; `--lenient` permits that explicitly unsupported
primitive and records its scope. A held lock, absent required guard, or failed
acquisition still refuses even with `--lenient`. Because CLI invocations are separate processes, retaining
exclusion across a capture requires the library API in a single process: enter
`medre.maintenance.stopped_declare.retain_local_exclusion` around the
`create_stopped_snapshot` call, so no local owner can start between the check
and the capture. A capture run without that retained exclusion is a capture
whose stop assertion was only ever checked point-in-time.

## What each provider declares

The provider descriptions below require an adapter distribution exposing its
matching declaration. Check the installed revision before relying on these
scopes; a distribution without that symbol is unsupported by automatic planning:

- **simplexchat** — handoff and inbox ledgers, the profile namespace
  database, the ownership guard inode, and both native profile databases
  (`<prefix>_agent.db`, `<prefix>_chat.db`) as SQLite backups. A configured
  `database_key` needs a coherent provider export that preserves native state
  and adapter ledgers. Automatic planning is unsupported for that case; a
  core-only selection must not be treated as a complete SimpleX backup.
- **chatmail** — native account directories come from the account registry;
  the selected numeric account ID is not a directory name. The native core
  uses the registry-assigned directory and `dc.db`. A declaration that guesses
  `<account_id>/db.sqlite` is not qualified for capture. Resolve the exact
  account store and attachment/export requirements before relying on a plan;
  the pending-send journal must be preserved with that native state.
- **lxmf** — worker identity and handoff-journal state are distinct from the
  shared Reticulum state. RNS stores named binary identity/path records; a
  guessed `storage/database` SQLite file is not a complete native inventory.
  Qualify the actual Reticulum state or provide a coherent export before
  describing a worker-file snapshot as a complete LXMF installation backup.
- **matrix** — the SDK-derived durable store database when the account and
  device are configured. Standard account IDs contain characters the
  inventory path grammar rejects, so generic capture is declared as requiring
  a provider export rather than guessed; the access-token sidecar under the
  XDG configuration home is likewise export-only. No maintenance acquisition of the SDK store lease is implemented by the
  declaration. `check` therefore reports unsupported local exclusion; that
  does not mean the SDK itself lacks a store ownership lease.
- **meshtastic** and **meshcore** — device-attached: no local files exist,
  and radio identity lives in firmware. Nothing is captured for them, by
  declaration rather than by omission.
- **xmpp** — stateless locally; the credential lives in configuration or an
  environment override and is never part of a state snapshot.

Each plan summary lists every configured provider with its class —
`file_backed`, `externally_managed`, `device_attached`, `stateless_local` —
and its notes. A provider whose installed distribution predates the
declaration symbol, or whose declaration fails to load, reports as
`unsupported` and makes `plan` refuse; nothing is captured on the strength of
a guessed filename.

## Practical scenario

1. Stop MEDRE and every native worker. `--stopped` remains an operator
   assertion; it is not proof that any process has exited.
2. Run `check` to assess the implemented provider exclusion primitives. To
   close the check-to-capture race, drive the capture through the library
   with `retain_local_exclusion` held for its duration.
3. Run `plan`, review the summary against your actual installation, and keep
   the inventory with the installation's configuration.
4. `capture` with `--root` mapped so every declared file is inside it, then
   `verify`, then `restore` into an absent destination only.
5. Before activation, restore any export-only state (operator-supplied
   identities, credentials sidecars, provider exports) and re-verify that the
   original installation cannot start elsewhere. Restoring bytes never
   re-establishes a live lock; the next owner re-fences through the same
   paths.

Capture and restore remain maintenance operations without sends. Unknown or
uncertain native handoffs stay held after a restore; recovery must still use
the existing outbox claim and packet evidence checks before anything is
retransmitted.
