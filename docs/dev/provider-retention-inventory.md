# Provider-owned retention inventory

This page is the per-provider discovery inventory asked for by
[contract gap G06](contract-gap-status.md): which stores exist around one
MEDRE installation, which bytes the core owns, which the transport SDK or
its provider owns, which live remotely on other parties' systems, what has
to be preserved for the bridge to keep working, and what local cleanup can
and cannot reach. It is an inventory only. It records no cleanup capability,
implements none, and promises none; every erase-ability verdict below is a
statement about reachability, not about behavior that exists today.

## Method and its limits

Every per-transport row below was read from source at a fixed revision,
not from prose alone. Core was read at `fcbed4763a9b2b75ed02511775321ce2a4002ee0`
(`main`). The adapter distributions were read at their `main` heads:
matrix `ef9603e`, meshtastic `0f5540b7`, meshcore `b68155d8`, lxmf
`3d0c451e`, xmpp `07d69eb3`, chatmail `5216dcd`, simplexchat `99524139`.
Paths in citations are relative to the owning repository, so `core`
prefixed paths are this repository and `matrix:` prefixed paths are
`medre-adapter-matrix`, and so on.

Three limits follow from source reading alone. First, transport SDK
internals are outside these repositories: where a fact depends on what a
pinned SDK does inside its own store, the row says so and names what would
establish it. Second, remote systems (homeservers, chatmail servers, SMP
servers, XMPP servers, propagation nodes, mesh peers) retain bytes under
policies no repository in this set controls; the inventory can only state
that the copies exist in principle, never their size, age, or retention
window. Third, nothing here was executed: no SDK was started, no account
was contacted, and no store was enumerated on disk, so every classification
is a reading of declarations, configuration dataclasses, and runtime code,
not an observed store.

Evidence markers used in the matrices:

- `decl` — a provider stopped-state declaration or config dataclass in the
  adapter distribution (`core:src/medre/maintenance/stopped_declare.py`
  defines the seam at line 163; adapters ship `stopped_declaration.py`
  modules consumed through the adapter spec's
  `stopped_state_declaration` symbol, `core:src/medre/adapter_spec.py:162`).
- `code` — runtime source in the adapter or core distribution.
- `contract` — a spec or operations page in the owning repository.
- `n/e` — not established from these repositories; the open-questions
  section names what would establish each such row.

## What core owns and what the ceiling sweep touches

Core owns one SQLite catalog at `{state_dir}/medre.sqlite`
(`core:src/medre/config/paths.py:312-337`), sanitized log files under the
log directory (`core:docs/spec/security-privacy.md` section 1.4), and
run-session smoke snapshot JSON written to the storage directory by default
(`core:src/medre/cli/main.py:546-551`). The store-by-store census in
`core:docs/spec/content-privacy-ceiling.md` section 3 is the authority for
which tables hold event-attributable content: bodies, excerpts, wire plans
(`radio_attempt_progress.plan_json`/`parts_json`), outbox dispatch contexts,
attachment associations and plaintext blobs, and the SQLite free pages and
WAL frames behind all of them. Enforcement purges the `purged` and
`cleared-on-surviving-rows` census entries, verifies a residual battery,
and records a content-free receipt; the registry contract
`STORAGE_COLUMN_CLASSES` at `core:src/medre/core/storage/classification.py:78`
is the column-level authority, and the sweep operates only on that core
catalog.

Three census entries are explicitly unmeasured and none of them is
provider-owned: `adapter_checkpoints` (adapter-scoped cursor and metadata,
no event attribution), `plugin_state`, and SQLite free pages and WAL frames
(`core:docs/spec/content-privacy-ceiling.md:114-120`). The section 5
guarantee is that logical purge is not physical erasure; the operator
erasure step (`VACUUM` under quiescence, then
`PRAGMA wal_checkpoint(TRUNCATE)`) covers only this database file, and the
spec states plainly that neither method erases backups, earlier file
snapshots, or provider-owned copies
(`core:docs/spec/content-privacy-ceiling.md:275-276`), which remain
"independently retained" per `core:docs/spec/content-retention.md:111-116`
and `core:docs/spec/security-privacy.md:166-168`.

## What snapshots and exports copy

The stopped-state maintenance surface copies bytes; it deletes none. A
capture takes the core database as a SQLite backup with committed WAL, plus
explicit adapter-ledger and native-identity entries, hashes all selected
bytes, and publishes a complete directory only after verification
(`core:docs/ops/stopped-state-snapshots.md:20-27`). Snapshots are
unencrypted and can contain native profile secrets; the restore path
compares receipt high-water marks against a stopped live database when
asked, and refuses divergence
(`core:docs/ops/stopped-state-snapshots.md:39-55`). The derived per-provider
inventories (`core:docs/ops/stopped-state-provider-inventory.md:79-117`)
name, per transport, exactly which files each provider considers its own —
and label anything that cannot honestly be captured by files, such as radio
firmware or credential sidecars, as export-only or device-attached instead
of silently omitting it. The qualification table in
`core:docs/ops/mixed-installation-restore-qualification.md:14-17` states the
same split from the restore side. Every snapshot therefore extends the
lifetime of provider-owned bytes to wherever the snapshot is stored; that
copy is operator-owned and outside every sweep in the system.

## Per-transport scope matrices

Columns: the store; its owner; whether it can hold message content or
bytes derived from it; what must be preserved for the bridge to keep
working; the cleanup or refusal behavior that exists today in source;
erase-ability from this host. Owner values: `core`, `adapter` (this
distribution's own files), `SDK` (the pinned transport SDK's local store),
`device` (attached radio firmware), `remote` (another party's system).
Erase-ability uses the fixed vocabulary: logical purge possible (core
sweep), provider-native tooling required, not erasable locally, or remote
copy out of reach. Security limits and refusals are summarized under each
matrix from the declarations themselves.

### Matrix

| Store                                                                                                                                                                                           | Owner                        | Content                                    | Preservation need                                                                                             | Existing cleanup or refusal                                                                                                                                                                            | Erase-ability                                                                                                        |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| SDK account database `<user>_<device>.db` under `{state}/adapters/{id}/matrix/store` (default `store_path`): durable sync, crypto and cross-signing state                                       | SDK                          | yes (sync timeline captures, crypto state) | device keys, cross-signing, sync position; erasing it drops the device identity and forces fresh verification | declaration captures it for snapshot only; `.ingest.lock` flock retained, never created or seized (`matrix:src/medre/adapters/matrix/stopped_declaration.py:102-118`)                                  | not erasable locally without destroying the crypto identity; provider-native tooling required                        |
| Device-trust sidecars `<stem>.trusted_devices` / `.blacklisted_devices` / `.ignored_devices` beside the database                                                                                | SDK                          | no (trust lists)                           | peer device trust decisions                                                                                   | declaration selects them when present; absent files are not manufactured (`matrix:src/medre/adapters/matrix/stopped_declaration.py:146-157`)                                                           | logically deletable files, but deletion resets trust decisions; provider-native tooling required for coherent change |
| Access-token sidecar `$XDG_CONFIG_HOME/medre/credentials/matrix.json` (`matrix:src/medre/config/adapters/matrix_credentials.py:26-32`)                                                          | adapter                      | no (credential)                            | required to authenticate; losing it forces re-login                                                           | sidecar is a capture input only; no code deletes it                                                                                                                                                    | logically deletable file; re-authentication re-creates it                                                            |
| Inbound attachment bytes: bounded authenticated download, decrypted, then handed to core admission; no adapter-local cache is written (`matrix:src/medre/adapters/matrix/adapter.py:1709-1800`) | core (after admission)       | yes                                        | none once admitted; core census purges `attachment_blobs`                                                     | core ceiling sweep purges; the adapter keeps retry state for fetch deferrals keyed by native identity (`matrix:src/medre/adapters/matrix/adapter.py:1718-1722`)                                        | logical purge possible (core census `attachment_blobs`)                                                              |
| Homeserver room history, account data, device key uploads, media repository copies of shared attachments                                                                                        | remote (homeserver operator) | yes                                        | account and device records must survive for the bridge to reconnect                                           | none and none possible from here; the adapter never sends deletions of remote media, and declares native deletes only for its own outbound messages (`matrix:src/medre/adapters/matrix/adapter.py:95`) | remote copy out of reach; homeserver retention policy is n/e (open questions)                                        |

Security limits: capture refuses symlink or hard-link aliases on any
selected file, path separators in the account identity, and oversized or
malformed credential sidecars
(`matrix:src/medre/adapters/matrix/stopped_declaration.py:58-64,161-170`);
coloned SDK file names require a POSIX restore host
(`matrix:docs/spec/stopped-state.md`, "Selected account state").

### Meshtastic

| Store                                                                                                                                | Owner               | Content                                            | Preservation need                                                                                                                                             | Existing cleanup or refusal                                                                                                                                                                                                          | Erase-ability                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------ | ------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| Radio firmware state: node database, channel keys and settings on the attached radio                                                 | device              | no content copies; holds network identity and keys | radio identity and channel keys; a factory reset or destructive reflash erases them (`meshtastic:src/medre/adapters/meshtastic/stopped_declaration.py:19-26`) | declaration captures zero files and says so (`meshtastic:src/medre/adapters/meshtastic/stopped_declaration.py:34-50`); no local ownership primitive exists (`meshtastic:src/medre/adapters/meshtastic/stopped_declaration.py:52-64`) | not erasable locally; provider-native tooling required (radio-side reset or reflash) |
| In-memory node database mirrored from the radio during a session (`meshtastic:src/medre/adapters/meshtastic/session.py:218,674-677`) | SDK (volatile)      | no                                                 | none; rebuilt from the radio on reconnect                                                                                                                     | process exit discards it                                                                                                                                                                                                             | not applicable (memory only)                                                         |
| Outbound pacing queue (`meshtastic:src/medre/adapters/meshtastic/queue.py:1-14`)                                                     | adapter (volatile)  | yes until sent                                     | none                                                                                                                                                          | in-memory; cancelled items surface to the caller                                                                                                                                                                                     | not applicable (memory only)                                                         |
| Packets already propagated over the mesh to other nodes and their retained node tables                                               | remote (mesh peers) | yes                                                | n/a                                                                                                                                                           | none                                                                                                                                                                                                                                 | remote copy out of reach                                                             |
| Fake connectivity journals                                                                                                           | adapter (volatile)  | yes                                                | none                                                                                                                                                          | memory only (`meshtastic:src/medre/adapters/meshtastic/stopped_declaration.py:36-43`)                                                                                                                                                | not applicable                                                                       |

Security limits: the declaration classifies the transport `device_attached`
and instructs the operator to verify the TCP endpoint (which may be remote)
or the serial/Bluetooth device and its exclusive use before restart
(`meshtastic:src/medre/adapters/meshtastic/stopped_declaration.py:52-64`).
The adapter declares `deletes="unsupported"` (`meshtastic:src/medre/adapters/meshtastic/adapter.py:284`).

### MeshCore

| Store                                                                                                                                                          | Owner               | Content                                            | Preservation need                            | Existing cleanup or refusal                                                                                                                                                                               | Erase-ability                                          |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- | -------------------------------------------------- | -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ |
| Companion firmware state on the attached radio: node identity, contacts, channel keys                                                                          | device              | no content copies; holds network identity and keys | node identity and contact table for routing  | declaration captures zero files (`meshcore:src/medre/adapters/meshcore/stopped_declaration.py:33-49`); no local ownership primitive (`meshcore:src/medre/adapters/meshcore/stopped_declaration.py:51-67`) | not erasable locally; provider-native tooling required |
| In-memory contacts cache populated from `CONTACTS` events (`meshcore:src/medre/adapters/meshcore/session.py:729-732`)                                          | SDK (volatile)      | no                                                 | none; repopulated from the radio             | process exit discards it                                                                                                                                                                                  | not applicable (memory only)                           |
| In-memory dedup identity derived from wire fields (`meshcore:src/medre/adapters/meshcore/identity.py:1-26`)                                                    | adapter (volatile)  | digests only                                       | none                                         | memory only                                                                                                                                                                                               | not applicable                                         |
| Configured `identity`/`pubkey` strings and `node_config`; secret keys are forbidden in config (`meshcore:src/medre/config/adapters/meshcore.py:57-61,106-114`) | adapter (config)    | no (identity labels; secrets refused)              | node name and public key used for addressing | validation refuses `private_key`/`secret`/`password` keys in `node_config`                                                                                                                                | logically editable configuration                       |
| Packets propagated across the mesh to peers                                                                                                                    | remote (mesh peers) | yes                                                | n/a                                          | none                                                                                                                                                                                                      | remote copy out of reach                               |
| Fake connectivity journals                                                                                                                                     | adapter (volatile)  | yes                                                | none                                         | memory only (`meshcore:src/medre/adapters/meshcore/stopped_declaration.py:36-43`)                                                                                                                         | not applicable                                         |

Security limits: same device_attached class and exclusive-use instructions
as Meshtastic (`meshcore:src/medre/adapters/meshcore/stopped_declaration.py:51-67`);
a BLE connection slot can remain held by companion firmware until the host
releases the link (`meshcore:src/medre/adapters/meshcore/session.py:971-978`).
`deletes="unsupported"` (`meshcore:src/medre/adapters/meshcore/adapter.py:116`).

### LXMF

| Store                                                                                                                                                                                                                                                                                                                                                                               | Owner                              | Content                              | Preservation need                                                                                                                                           | Existing cleanup or refusal                                                                                                                                                                                   | Erase-ability                                                                                                          |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Endpoint private identity file, 64 bytes; configured `identity_path`, default `storage_path/.medre-identity` (`lxmf:src/medre/config/adapters/lxmf.py:99-102`; `lxmf:src/medre/adapters/lxmf/stopped_declaration.py:24,64`)                                                                                                                                                         | adapter                            | no (key material)                    | the bridge's LXMF identity; unrecoverable if lost                                                                                                           | declared `native_identity`, never cleaned                                                                                                                                                                     | not erasable without losing the endpoint identity                                                                      |
| Router storage directory from configured `storage_path`: `LXMRouter` persistent message and peer storage (`lxmf:src/medre/config/adapters/lxmf.py:110-114`): `lxmf/state.db` (+WAL/SHM), `local_deliveries`, `locally_processed`, `node_stats`, `outbound_stamp_costs`, `available_tickets`, `peers`, `*.imported` markers (`lxmf:src/medre/adapters/lxmf/stopped_layout.py:19-40`) | SDK                                | yes (native message and peer state)  | duplicate-suppression and stamp state; `state.db` required if sidecars or imported markers exist (`lxmf:src/medre/adapters/lxmf/stopped_layout.py:159-163`) | declaration selects the exact file set for snapshot; unknown files, interrupted temporary writes, and local propagation-node stores refuse selection (`lxmf:src/medre/adapters/lxmf/stopped_layout.py:90-97`) | provider-native tooling required; contents outside core census                                                         |
| Private ratchet files `lxmf/ratchets/<hex32>.ratchets`, signed native private ratchet history (`lxmf:src/medre/adapters/lxmf/stopped_layout.py:176-196`)                                                                                                                                                                                                                            | SDK                                | no (key material)                    | decryption continuity for ratcheted conversations                                                                                                           | declared `native_identity`; unrecognized ratchet names refuse (`lxmf:src/medre/adapters/lxmf/stopped_layout.py:182-184`)                                                                                      | not erasable without breaking ratchet continuity                                                                       |
| Durable handoff journal `.medre-handoffs.sqlite` beside the identity (`lxmf:src/medre/adapters/lxmf/stopped_declaration.py:22,70-74`)                                                                                                                                                                                                                                               | adapter                            | no (fences)                          | accepted/uncertain handoff fences; acceptance evidence                                                                                                      | captured as `adapter_ledger`; retained by design                                                                                                                                                              | logical purge possible in principle (core census covers only core tables; this ledger is adapter-owned and outside it) |
| Worker lease `.medre-worker.lock` and identity flock (`lxmf:src/medre/adapters/lxmf/stopped_declaration.py:112-128`)                                                                                                                                                                                                                                                                | adapter                            | no                                   | lock inodes are validated but not copied                                                                                                                    | retained during capture; released on exit                                                                                                                                                                     | logically deletable file; recreated by the worker                                                                      |
| Reticulum configuration and network-service state, discovered at `/etc/reticulum` or the user config directory unless `reticulum_config_dir` is set (`lxmf:src/medre/config/adapters/lxmf.py:103-108`)                                                                                                                                                                              | remote / other local service       | interface config, no message content | required for connectivity; independently backed up                                                                                                          | explicitly outside the endpoint selection (`lxmf:src/medre/adapters/lxmf/stopped_declaration.py:103-109`)                                                                                                     | not erasable by this distribution; owned by its own service                                                            |
| Messages held by remote propagation nodes (configured `outbound_propagation_node`, `lxmf:src/medre/config/adapters/lxmf.py:86-91`) until collected                                                                                                                                                                                                                                  | remote (propagation node operator) | yes                                  | n/a                                                                                                                                                         | none; local propagation-node stores are refused for endpoint capture (`lxmf:src/medre/adapters/lxmf/stopped_declaration.py:107`)                                                                              | remote copy out of reach; retention policy n/e                                                                         |

Security limits: endpoint paths must be canonical absolute paths without
symlinks; files over the 1 GiB bound and hard-linked state refuse; the
selection stops at the first unknown file name
(`lxmf:src/medre/adapters/lxmf/stopped_layout.py:46-88`). Leases do not
exclude other hosts or non-cooperating applications
(`lxmf:src/medre/adapters/lxmf/stopped_declaration.py:127`). Capabilities
declare `deletes="unsupported"` and `store_and_forward=True`
(`lxmf:src/medre/adapters/lxmf/adapter.py:92,95`).

### XMPP

| Store                                                                                                                                                                                 | Owner                     | Content                                      | Preservation need                                                                                                                                                       | Existing cleanup or refusal                                                                                                                             | Erase-ability                                         |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- | -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Locally: none. The adapter declares `stateless_local` with zero files (`xmpp:src/medre/adapters/xmpp/stopped_declaration.py:33-41`)                                                   | —                         | —                                            | the JID credential, which lives in configuration or an environment override and is never part of a snapshot (`xmpp:src/medre/adapters/xmpp/stopped_declaration.py:2-7`) | nothing to clean; no local ownership primitive exists (`xmpp:src/medre/adapters/xmpp/stopped_declaration.py:43-53`)                                     | not applicable                                        |
| Inbound stanza queue, bounded by `inbound_queue_size` (`xmpp:src/medre/adapters/xmpp/config.py:62`)                                                                                   | adapter (volatile)        | yes until processed                          | none                                                                                                                                                                    | memory only                                                                                                                                             | not applicable                                        |
| MUC history: joins request zero history (`maxstanzas=0`, `xmpp:src/medre/adapters/xmpp/connection.py:127`; `xmpp:README.md:81`) and delayed history is excluded (`xmpp:README.md:83`) | remote (server and rooms) | yes (server-side archives per server policy) | n/a                                                                                                                                                                     | the adapter never fetches archives; it cannot retract server copies and declares `deletes="unsupported"` (`xmpp:src/medre/adapters/xmpp/adapter.py:38`) | remote copy out of reach; server retention policy n/e |
| Account, roster, and archives on the XMPP server                                                                                                                                      | remote (server operator)  | yes                                          | account must survive to reconnect                                                                                                                                       | none                                                                                                                                                    | remote copy out of reach                              |

Security limits: the credential is never captured into state snapshots;
another host may use the same account concurrently and server resource
rules are not an exclusive ownership lock
(`xmpp:src/medre/adapters/xmpp/stopped_declaration.py:19-23`). The pinned
SDK is slixmpp at the manifest pin (`xmpp:pyproject.toml:8`); slixmpp's own
local persistence behavior, if any, is n/e (open questions).

### Chatmail

| Store                                                                                                                                                                                                                                          | Owner                             | Content            | Preservation need                                                                                                                                                               | Existing cleanup or refusal                                                                                                                                         | Erase-ability                                                                          |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- | ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| Native account registry `accounts.toml` with the id/dir/uuid mapping, under configured or default `accounts_dir` (`chatmail:src/medre/adapters/chatmail/config.py:141-152`; `chatmail:src/medre/adapters/chatmail/stopped_declaration.py:3-8`) | SDK (Delta Chat RPC core)         | no (mapping)       | the account UUID mapping; deleting it orphans the profile                                                                                                                       | declared `native_identity`; malformed, multi-account, or unrecognized registries refuse (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:133-221`)     | not erasable without losing the account binding                                        |
| Account database `dc.db` with committed WAL in the UUID directory: Delta Chat profile (messages, contacts, keys)                                                                                                                               | SDK                               | yes                | encryption keys and account state; erasure destroys the profile                                                                                                                 | capture only, as a SQLite backup; `dc.db` sidecars validated, blobs directory must be empty (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:223-247`) | provider-native tooling required (the SDK owns the schema; the adapter never opens it) |
| Blob directory `dc.db-blobs` (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:228-241`)                                                                                                                                           | SDK                               | yes when non-empty | adapter declares `attachments=False` (`chatmail:src/medre/adapters/chatmail/adapter.py:45`); the qualified capture boundary requires it empty, and a nonempty directory refuses | nonempty blob state refuses capture (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:236-238`)                                                         | logically deletable only through SDK-native paths; adapter never writes blobs          |
| Adapter pending-send journal `state/chatmail-pending.sqlite` (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:253-263,283-287`)                                                                                                   | adapter                           | no (fences)        | accepted/unknown handoff fence; acceptance evidence                                                                                                                             | captured as `adapter_ledger`; retained by design                                                                                                                    | adapter-owned file outside the core census                                             |
| Profile locks: native `accounts.lock` (fd_lock, `chatmail:src/medre/adapters/chatmail/stopped_declaration.py:320`) and the MEDRE lease beside the accounts directory (`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:57-61`)     | adapter / SDK                     | no                 | retained inodes, never copied as live locks                                                                                                                                     | retained during capture, released on exit                                                                                                                           | lock files; not content                                                                |
| Account and messages on the chatmail server until the client fetches and removes them                                                                                                                                                          | remote (chatmail server operator) | yes                | account credentials inside `dc.db` must survive to fetch                                                                                                                        | none                                                                                                                                                                | remote copy out of reach; server retention policy n/e                                  |

Security limits: configuration-only `plan` refuses outright; native
metadata is read only under retained owner exclusion in
`resolve_coverage`; symlinks, hard links, hot journals, unknown files, and
multi-account registries refuse
(`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:133-263`).
The native layout contract is read from the Delta Chat core source
referenced in the declaration docstring at the manifest pins
(`chatmail:src/medre/adapters/chatmail/stopped_declaration.py:3-8`).

### SimpleX

| Store                                                                                                                                                                                                                                                                            | Owner                        | Content                          | Preservation need                                                                    | Existing cleanup or refusal                                                                                                                                                                                                         | Erase-ability                                               |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------- | -------------------------------- | ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| Native agent database `<prefix>_agent.db`: connections, identity (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:84-88`); prefix defaults under `{state}/adapters/{id}/simplexchat/profile` (`simplexchat:src/medre/adapters/simplexchat/config.py:112-119`) | SDK                          | no (connection and key material) | the profile's connection identities; erasure severs all contacts                     | capture as `native_identity` SQLite backup; `database_key` (encrypted SQLCipher) profiles refuse automatic planning and need a coherent provider export (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:45-57`) | provider-native tooling required; the SDK owns the schema   |
| Native chat database `<prefix>_chat.db`: profile, contacts, groups (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:90-94`); retained history is walked page-wise on restart recovery (`simplexchat:docs/spec/simplexchat.md:52`)                             | SDK                          | yes                              | contacts, groups, and message history used for dedup and recovery                    | capture only; the adapter never mutates it                                                                                                                                                                                          | provider-native tooling required                            |
| Native cache directory, default under the XDG cache home (`simplexchat:src/medre/adapters/simplexchat/config.py:117-119`)                                                                                                                                                        | SDK                          | disposable per declaration       | none                                                                                 | "disposable and is never selected" (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:101-105`)                                                                                                                    | logical purge possible (declared disposable)                |
| Adapter ledgers `handoffs.sqlite` (queued-send dedup), `inbox.sqlite` (inbound dedup), and the profile handoff namespace `<prefix>.medre-owner.sqlite` (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:58-76`)                                               | adapter                      | no (dedup identity and fences)   | acceptance evidence and dedup state; recovery requires both ledgers and native state | captured as `adapter_ledger`; retained by design                                                                                                                                                                                    | adapter-owned files outside the core census                 |
| Ownership guard `<prefix>.medre-owner.lock` (flock inode)                                                                                                                                                                                                                        | adapter                      | no                               | the inode fences profile admission; copied bytes are never a live lock               | retained during capture, never created or seized (`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:113-132`)                                                                                                      | lock inode; not content                                     |
| Messages in SMP server queues until the recipient's agent delivers and deletes them                                                                                                                                                                                              | remote (SMP server operator) | yes                              | n/a                                                                                  | none; the adapter never touches server queues and declares `deletes="unsupported"` (`simplexchat:src/medre/adapters/simplexchat/adapter.py:38`)                                                                                     | remote copy out of reach; server-side deletion behavior n/e |

Security limits: simultaneous MEDRE ownership of a profile is refused; the
flock guard is reused, never seized
(`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:113-132`);
another host with the same profile cannot be excluded locally
(`simplexchat:src/medre/adapters/simplexchat/stopped_declaration.py:97-100`).
The pinned SDK is simplex-chat at the manifest pin
(`simplexchat:pyproject.toml:9`).

## Cross-transport summary

| Transport  | Local SDK store                                       | Adapter-owned files                               | Device/remote copies                       | Content-bearing locally outside core  | Deletes capability declared                               |
| ---------- | ----------------------------------------------------- | ------------------------------------------------- | ------------------------------------------ | ------------------------------------- | --------------------------------------------------------- |
| Matrix     | SQLite account store + trust sidecars + token sidecar | none beyond the token sidecar                     | homeserver history and media               | no (attachments go to core)           | native (`matrix:src/medre/adapters/matrix/adapter.py:95`) |
| Meshtastic | none (device_attached)                                | none                                              | radio firmware; mesh peers                 | no                                    | unsupported                                               |
| MeshCore   | none (device_attached)                                | config identity labels only                       | companion firmware; mesh peers             | no                                    | unsupported                                               |
| LXMF       | router storage tree + ratchets                        | identity file, handoff journal, worker lock       | Reticulum service state; propagation nodes | yes (router message and peer storage) | unsupported                                               |
| XMPP       | none (stateless_local)                                | none                                              | server account, rooms, archives            | no (zero-history joins)               | unsupported                                               |
| Chatmail   | `dc.db` + `dc.db-blobs` + registry                    | pending-send journal                              | chatmail server mailbox                    | yes (`dc.db`; blob dir must be empty) | unsupported                                               |
| SimpleX    | `_agent.db` + `_chat.db`; disposable cache dir        | handoffs/inbox ledgers, owner namespace and guard | SMP server queues                          | yes (`_chat.db` history)              | unsupported                                               |

Two structural facts stand out. Only the Matrix adapter declares a native
delete capability, and it applies to that adapter's own outbound messages,
not to provider-held copies. And only three transports keep message content
in local SDK stores at all (Matrix sync captures, LXMF router storage,
Chatmail and SimpleX native databases); the two radios keep none locally and
hold their identity-bearing state on the attached device instead.

## What cannot be erased and why

- Remote services keep their own copies. Homeserver room history, media
  repository objects and device key records; chatmail server mailboxes;
  SMP server queues; XMPP server archives and MUC history; LXMF messages
  held on propagation nodes — all live on other parties' systems under
  policies this software does not control. Nothing in any repository in
  this set can reach them, and no local deletion retracts them.
- Mesh peers retain what they received. A radio packet delivered over a
  mesh has been copied to other nodes; those nodes' firmware-held node
  tables and any retained packets are outside every local store.
- Device firmware state is not host state. Meshtastic and MeshCore radio
  identity, channel keys, and contact tables live in flash on the radio;
  changing them requires radio-side operations (with the documented risk
  that factory reset or destructive reflash erases them), not file
  operations on the host.
- Old backups and snapshots persist. Every stopped-state snapshot, export,
  or file-level copy made before any future purge retains the bytes it
  captured, unencrypted, wherever the operator stored it
  (`core:docs/ops/stopped-state-snapshots.md:16-18`). The core spec makes
  the same point for its own backups
  (`core:docs/spec/content-privacy-ceiling.md:275-276`).
- Physical SQLite remnants outlive logical deletion even in core, and no
  secure-delete pragma is enabled; byte-level erasure is the explicit
  operator step, and it covers only the core database file
  (`core:docs/spec/content-privacy-ceiling.md:250-276`).
- Crypto stores and identities resist erasure by design. Deleting a Matrix
  account database, an LXMF identity or ratchet set, a Chatmail profile, or
  a SimpleX agent database does not perform a privacy operation; it
  invalidates the bridge identity and can sever conversations without
  touching any remote copy. The declarations therefore refuse to fabricate
  or quietly drop these files, and capture treats them as
  `native_identity` inputs to a snapshot, never as cleanup targets.

## Per-provider open questions

Each item states the fact that source reading could not establish and the
concrete step that would establish it. These are unknowns, not planned
work.

- Matrix: what a homeserver retains for this account (room history,
  media objects, device key records) and under what server-side policy.
  Establishing it requires observing a controlled homeserver fixture's
  retention at the manifest pins, or reading the pinned server's retention
  configuration — neither is in these repositories. Likewise, the exact
  byte layout of the pinned `mindroom-nio` store database beyond the
  declared file names requires reading that SDK's source at its pin.
- Meshtastic: what firmware actually persists per node type (node
  database size, key slots, retained packet state) across the configured
  connection types. Establishing it requires device-side enumeration on
  the configured firmware at the pinned SDK revision; the adapter
  distribution states only that the state is device-resident and that a
  factory reset may erase it.
- MeshCore: which contact and identity records the companion firmware
  persists, and whether any packet state is retained across power cycles.
  Same establishment path as Meshtastic: device-side enumeration at the
  pinned companion-firmware revision.
- LXMF: whether the router's `local_deliveries` and `state.db` hold full
  message bodies or metadata only, and how long, under the pinned LXMF
  revision. The adapter's config contract calls the directory persistent
  message and peer storage, but the schema is the SDK's; reading the
  pinned `LXMRouter` source, then exercising a disposable endpoint, would
  establish it. Remote propagation-node retention windows are likewise
  n/e from these repositories.
- XMPP: whether the pinned slixmpp client persists anything locally
  beyond memory, and what a typical server retains (MAM archives for
  direct chats and MUC rooms) after this adapter's zero-history joins.
  Establishing it requires reading the pinned slixmpp source and
  observing a controlled server fixture's archive behavior.
- Chatmail: what the chatmail server retains per account and for how
  long after fetch, and what Delta Chat core writes inside `dc.db`
  beyond the declared files. Both require reading the pinned Delta Chat
  core source and observing a controlled chatmail server fixture.
- SimpleX: how SMP servers treat queues after delivery (deletion
  behavior and timing) and what the agent database retains about
  delivered queues. Establishing it requires reading the pinned
  simplex-chat agent source and observing a controlled SMP fixture.

## No cleanup capability exists here

This inventory records ownership, preservation needs, refusal behavior,
and reachability. No code in any repository in this set deletes a
provider-owned store: the ceiling sweep purges only the core census
stores; the stopped-state surface only copies and restores; the adapters
declare deletes `unsupported` everywhere except Matrix's own outbound
message redaction; and no global "delete every provider copy" control
exists in any repository read for this page. Any future per-provider
cleanup would be a separate, provider-native, refusal-tested change; this
document is not its design and makes no promise on its behalf.
