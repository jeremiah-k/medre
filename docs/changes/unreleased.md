# Unreleased Changes

Pre-release MEDRE. All changes below are unreleased and subject to change
without notice. Append new entries to the bottom of this file — do **not**
create per-commit fragment files.

---

## Breaking Changes

- **Config format is YAML-only.** `medre.toml` / `config.toml` must be
  renamed to `.yaml` / `.yml`. The loader rejects `.toml` with a clear
  migration error. A leftover TOML file in an auto-discovery directory now
  surfaces a migration pointer instead of a confusing "not found" error.
  `RouteConfig.from_toml_dict` / `RouteConfigSet.from_toml_dict` were
  renamed to `from_dict` (dict shape unchanged).
- **Removed `meshnet_name` and `matrix_relay_prefix` from
  `MeshtasticConfig`.** Rename `meshnet_name` to `origin_label` and move
  `matrix_relay_prefix` to `MatrixConfig.relay_prefix`. `{origin_label}`
  is the single MEDRE-generic source label.
- **Attribution surface uses canonical template variables only.** The old
  placeholders (`{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
  `{meshnet_name}`) are unknown and rendered as literal text. Canonical
  variables: `{sender}`, `{sender_short}`, `{sender_id}`,
  `{sender_handle}`, `{platform}`, `{route_id}`, `{channel}`,
  `{origin_label}`.
- **Unknown config keys fail at load.** The loader rejects unknown keys at
  the root, adapter-instance, route, and section levels (`runtime`,
  `logging`, `storage`, `retry`, `runtime.limits`) with a
  `ConfigValidationError` naming the offending key and listing accepted
  keys. Non-mapping section values and unknown transport names
  (`adapters.matrixx`) are also rejected. JSON schemas set
  `additionalProperties: false` to match. Run `medre config check` to
  surface every rejection before startup.

## Operator Commands

- **`medre routes plan`** renders the expanded route topology offline — no
  adapter startup, no SDK import, no network or hardware I/O. Shows per-leg
  direction and platform pair, the effective `origin_label` with its
  provenance (`per_entry`, `route`, `adapter`, `unset`), allowed fan-in
  decisions, and duplicate-room ambiguity errors (exit non-zero).
- **`medre support bundle`** collects a redacted offline diagnostic ZIP
  for issue reports: config check result, expanded route plan, adapter
  summary, environment info, redacted config copy, and schema presence.
  Secret-named field values are replaced with `***REDACTED***` (keys
  preserved). On config-load failure it still writes a partial archive and
  exits 0; it exits 3 only when the ZIP write itself fails.
- **`medre config check` is a complete pre-flight gate.** Route adapter
  references are validated at check time (previously deferred to
  `medre run`). Example configs are validated by a focused CI step.
  Unknown-key errors append migration hints for recognized removed keys.
- **`medre storage status` / `medre storage reset`** manage the pre-release
  SQLite database. `status` opens the database read-only (usable on
  shape-mismatched databases) and reports the stored vs. expected schema
  version plus per-table missing columns. `reset` is destructive: backs up
  the database and `-wal`/`-shm` sidecars to a timestamped file, then
  deletes the originals; gated by `--yes` and SQLite magic-byte validation.
  `--storage-path` defaults to the resolved state directory.
- **Operator surface docs aligned with the CLI.** All top-level commands
  and their subcommands are documented. The support bundle (offline,
  redacted ZIP) and the storage-backed evidence report are cleanly
  separated; the previous "full diagnostic snapshot" overclaim for the
  support bundle is removed. `adapter matrix auth logout` is documented as
  not-yet-implemented (only `login` and `status` exist today).

## Config & Schema

- **Per-context origin labels for `channel_room_map`.** Each entry may
  carry its own `source_origin_label` / `dest_origin_label` alongside
  `room`. Precedence: per-entry → route → adapter → empty string. Explicit
  `""` suppresses fallback for that leg; an absent label falls through.
  Bare-string entries are unchanged.
- **Duplicate-room fan-in.** A `channel_room_map` may map multiple
  Meshtastic channels to one Matrix room for Meshtastic→Matrix fan-in.
  Duplicate Matrix rooms are rejected only when the route creates a
  Matrix→Meshtastic leg (ambiguous source); allowed otherwise.
- **Direction-aware route origin labels.** `source_origin_label` (forward
  legs) and `dest_origin_label` (reverse legs) replace the single
  `origin_label` route field. Both default to `None` (fall back to adapter
  `origin_label`). Per-channel origin labels are not implemented; use
  separate routes per channel.
- **YAML loader hardening.** Invalid-UTF-8 config files raise
  `ConfigFileError`. Exotic mapping key types (`!!omap`, `!!set`) raise
  `StrictYAMLError` in both the loader constructor and the post-parse type
  walk. The redacted `config.yaml` evidence artifact escapes control
  characters and quotes unsafe mapping keys for guaranteed round-trippable
  output.
- **Routing spec reconciled with the typed config model.** The
  `docs/spec/` schema, route, channel-mapping, and origin-label sections
  now match the typed model in `src/medre/config/`. No runtime behavior
  changed.
- **New attribution config fields:** `meshcore_relay_prefix`,
  `lxmf_relay_prefix`, and `origin_label` (string, default `""`) on all
  four adapter configs; `relay_prefix` (string, default `""`) on
  `MatrixConfig`. `announce_interval_seconds` (float, default `600.0`)
  configures periodic LXMF announce for mesh path discovery. Meshtastic
  packet classification policy is now configurable.

## Transport & Attribution

- **Transport capability semantics documented.** CapabilityLevel decision
  mapping, evidence signals, unknown event-kind passthrough, fail-closed
  behavior for unknown relation types, capability filtering during replay,
  and the `capability_suppressed` failure kind.
- **Transport-native identity enrichment.** Each adapter projects its
  native sender identity into the generic `RelayAttribution` sender
  fields; core rendering stays transport-neutral. Opaque identifiers
  (LXMF hash, MeshCore pubkey prefix) never populate `{sender}`;
  `{sender_id}` carries the opaque value. Identity enrichment is
  observational — not delivery evidence, not authoritative storage state,
  may be stale.
  - Matrix: MXID → `source_sender_id` / `source_sender_handle`; display
    name → `source_sender_label`; MXID localpart →
    `source_sender_short_label`.
  - Meshtastic: `from_id` → `source_sender_id`; node-database longname /
    shortname (read in-memory at ingress) → labels.
  - MeshCore: pubkey prefix → `source_sender_id`; local contact
    `adv_name` → label when the sender is a known contact.
  - LXMF: `source_hash` → `source_sender_id`; captured display name →
    labels.
- **Meshtastic metadata namespacing.** Identity keys
  (`meshtastic.from_id`, `.longname`, `.shortname`) and non-identity
  packet metadata (`meshtastic.packet_id`, `.channel`, `.portnum`,
  `.to_id`, `.is_direct_message`, `.reply_id`, `.emoji`, `.emoji_flag`)
  are now namespaced. Bare forms are retained as legacy input tolerance.
  Core relation enrichment sources sender labels exclusively from a
  generic `SenderProjectionFn` callback wired by the runtime builder.
- **LXMF announce-based display-name enrichment.** Announce-cache
  resolution populates `{sender}` for LXMF-origin events when the sender
  is a locally-known Reticulum identity. No network call; never raises.
  Precedence: message-carried `source_name` > announce-cache resolved >
  `None`.

## Adapter Lifecycle & Delivery

- **Queued delivery outbox correlation.** Exact `outbox_id` /
  `attempt_number` correlation for async queued adapters, stale callback
  protection, terminal queue outcome reporting. Removed
  `delivery_plan_id=None` legacy fallback.
- **Retry route-decision parity.** Route-decision metadata is persisted in
  outbox item metadata at creation time and recovered during retry
  reconstruction so retry delivery matches the original live decision.
- **Adapter ingress evidence parity.** Post-stop ingress hardened; LXMF
  diagnostics evidence gaps filled.
- **MeshCore BLE reconnect fix.** Linux BlueZ `le-connection-abort-by-local`
  errors no longer abort the initial connect, and stale BlueZ state no
  longer prevents reconnect. Per-contact retry timeout cache is cleared on
  reconnect and failed-start cleanup.
- **Adapter startup lifecycle cleanup.** Failed-start cleanup hardened
  across MeshCore, LXMF, and Meshtastic to match the Matrix pattern. The
  Matrix adapter rolls back lifecycle fields on failed start and emits the
  started log after completion.
- **Outbox lifecycle extracted.** Outbox lifecycle operations extracted
  from `PipelineRunner` into a dedicated `OutboxManager` module. Pure
  refactoring — no behavior changes.

## Support Bundle Internals

- **Serializer hardened.** Mixed `msgspec.Struct` and `dataclass` payloads
  serialise cleanly at any nesting depth. Tuples convert element-wise to
  lists; `dataclasses.asdict` output flows back through the recursive
  normalizer; `set` / `frozenset` normalise to sorted lists. The bundle
  remains offline and observational — no SDK imports, no network or
  hardware I/O, no redaction change.
- **Typed member models.** Manifest, config_source, config_check,
  environment, and schemas members use `msgspec.Struct`. The `SchemaEntry`
  failure shape emits explicit `null` keys alongside `present: false` for
  a stable four-key shape; changing it requires a `bundle_schema_version`
  bump. `adapters.json` stays a plain dict (conditionally-present fields).

## Documentation & Policy

- **Durable-language policy enforced tree-wide.** Internal
  development-process vocabulary is forbidden in all durable artifacts:
  docs, source comments and docstrings, test names, test filenames,
  example configs, branch names, and new commit messages. A scanner
  enforces the policy across `docs/`, `src/`, `tests/`, and `examples/`
  (content and filenames). Historical git commit messages are preserved.
- **Scanner coverage improved.** Patterns are constructed from string
  fragments so the blocked words never appear literally anywhere in the
  tree, including the scanner and enforcer files themselves. Numeric
  batch qualifiers and reviewer-role labels are now caught; existing
  labels across tests and audit docs were renamed to comply.
- **Stale active docs paths removed.** References to the removed
  `docs/contracts/` and `docs/runbooks/` legacy paths in root config
  files and CI scripts were removed or repointed at active `docs/ops/`
  paths. A regression test guards against their return.
- **Dead code removed.** Removed TOML credential mutation helpers from
  `src/medre/adapters/matrix/auth.py` after the YAML-only migration.
- **Dead test-file references resolved.** Four `tests/*.py` references in
  developer docs pointed at non-existent files; each was corrected to the
  existing target or labeled as external to MEDRE.
- **Docs reference guard.** `tests/test_docs_links.py` gained
  `TestDocTestReferencesResolve`, which scans `docs/**/*.md` for
  `tests/*.py` paths and fails when one does not resolve, with a narrow
  per-(file, regex) allow-list for explicitly-labeled historical and
  external-repo references.
