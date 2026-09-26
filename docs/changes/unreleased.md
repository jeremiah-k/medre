# Unreleased Changes

Pre-release MEDRE. All changes below are unreleased and subject to change
without notice. This file is the aggregate prerelease history: thematically
grouped entries below cover the early prerelease work, and the final section
consolidates the numbered-fragment era (150–185). Record new in-flight changes
as numbered fragments under `docs/changes/unreleased/`; consolidate them into
this file once they land.

---

## Breaking Changes

- **`channel_room_map` removed in favor of generic `context_map`.**
  Route-level endpoint mapping is now a map of opaque source contexts to
  structured entries (`dest_context` XOR `dest_destination`, plus optional
  per-entry origin labels); the platform-specific channel/room ontology and
  the `adapter_platforms` route-expansion parameter are gone. Expanded
  mapping legs use stable source-context tokens: `__map<token>__fwd` / `__map<token>__rev`. Old configs
  fail the generic unknown-key rejection with a hint toward
  `context_map`. See `docs/changes/unreleased/215-generic-context-route-mapping.md`.
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
  direction and transport pair, the effective `origin_label` with its
  provenance (`per_entry`, `route`, `adapter`, `unset`), allowed fan-in
  decisions, and duplicate-context ambiguity errors (exit non-zero).
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

- **Per-context origin labels for `context_map`.** Each structured
  entry may carry its own `source_origin_label` / `dest_origin_label`
  alongside its `dest_context` / `dest_destination`. Precedence:
  per-entry → route → adapter → empty string.
  Explicit `""` suppresses fallback for that leg; an absent label falls
  through. Bare-string context entries are rejected by the current prerelease
  contract.
- **Duplicate-context fan-in.** A `context_map` may map multiple source
  contexts to one `dest_context` for forward-only fan-in.
  Duplicate `dest_context` values are rejected only when the route creates
  reverse legs (ambiguous reverse-leg source); allowed otherwise.
- **Direction-aware route origin labels.** `source_origin_label` (forward
  legs) and `dest_origin_label` (reverse legs) replace the single
  `origin_label` route field. Both default to `None` (fall back to adapter
  `origin_label`). Structured `context_map` entries may override these
  labels per entry; general routes that need context-specific attribution
  still use separate routes per context.
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
- **Synapse integration image pinned to one source of truth.** The
  Synapse Docker image is now anchored consistently across the integration
  compose file, the Docker integration CI workflow, the integration test
  conftest default, the docker-bridge-artifacts runtime env-fallbacks, and
  the integration runner script. The compose file
  (`docker-compose.integration.yaml`) is canonical
  (`matrixdotorg/synapse:v1.155.0@sha256:...`); the CI workflow pins the
  same tag and digest; the conftest and runtime env-fallbacks carry the
  tag only (digest intentionally omitted — those defaults fire only for
  local runs without `MEDRE_SYNAPSE_IMAGE` set). A non-docker regression
  test statically scans each site with narrow per-file patterns and fails
  fast on future drift.

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
- **Meshtastic metadata namespacing.** Identity keys and non-identity
  packet metadata are stored under the versioned `native.meshtastic`
  namespace. Bare adapter-native metadata is not a supported persisted
  shape. Core relation enrichment sources sender labels exclusively from a
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
- **Diagnostics recovery behavior coverage.** Replaced two
  state-precondition tests in `test_session_diagnostics_state_hygiene.py`
  with tests that drive the production Matrix `_sync_with_reconnect` and
  LXMF `_reconnect_loop` recovery paths and assert stale reconnect state
  is cleared by production code, not just by setters.

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
- **Package-safe schema reporting.** `schemas.json` carries a
  `schema_source` field (`"source-tree"` when `docs/schemas/` is reachable,
  `"not-packaged"` under a wheel / site-packages install) so the absence of
  schema files and the example-config validator script in an installed
  package is reported as expected, not mistaken for schema drift. Per-entry
  `SchemaEntry` shapes, msgspec `$id` / `$schema` aliases, and the offline
  no-I/O contract are unchanged.

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
- **Stale command-surface test fixtures removed.** Dropped a skipped
  test class, a commented-out constant, and a redundant
  trace-event-config assertion that referenced the deleted
  `docs/architecture/operator-command-surface.md`. Live command-surface
  coverage continues from `docs/ops/configuration.md` via
  `tests/test_docs_command_surface.py` and the surviving assertions in
  `tests/test_command_surface_and_status_consistency.py`.
- **Exception suppression audit.** Classified fifteen silent swallowed
  exceptions in runtime/storage with `cleanup-silent` comments; follow-up
  inventory in adapters/ remains.
- **Matrix auth docs prefer stdin / interactive password entry.** The
  `adapter matrix auth login --help` epilog, `docs/ops/configuration.md`,
  and `docs/ops/install.md` now present the interactive prompt and
  `--password-stdin` as the preferred paths, and warn that `--password`
  is visible in shell history, process listings, and audit logs. The
  `--password` flag remains supported for automation that cannot pipe
  stdin. No login behavior changed; `src/medre/adapters/matrix/cli.py`
  is untouched.
- **Post-audit cleanup.** Matrix `--password-stdin` now tolerates stdin
  streams without a file descriptor (no crash on `io.StringIO` or
  process-substitution input). Documentation test-file references
  verified current. Durable-language enforcer prose neutralized.

## Dependencies

- **All dependencies pinned to exact versions; Renovate now bumps them.**
  Runtime, dev, and optional SDK extras moved from `>=` floors to exact
  pins. `pyproject.toml` is the human-readable version authority and the
  lockfile records the resolved artifacts; release notes deliberately do
  not duplicate the pin values because Renovate updates them independently.
  `renovate.json` sets `rangeStrategy: pin` so Renovate maintains the exact
  pins going forward instead of leaving `>=` floors untracked. Full
  resolution of every extra is verified via `uv pip install --dry-run`; all
  transport SDK extras import against the declared pins.
- **Renovate cannot pin Python policy fields.** The repo-wide pin strategy
  taught Renovate to pin `requires-python` to a single CPython release
  (`==3.14.7`) and to patch-pin the CI workflow `python-version`, which
  broke installation on every supported Python except 3.14. Both fields
  are deliberate support policy and are now excluded from Renovate
  management, with static guards enforcing the policy: `requires-python`
  must remain a `>=` floor, and workflow `python-version` values must
  stay minor-level. The legitimate pins in the same Renovate PR (GitHub
  Action refs, `setuptools` in build-system requires) remain enabled.

## Continuous Integration

- **Docker integration images aligned and Renovate-managed.** Renovate
  regex custom managers now update the Synapse and meshtasticd
  integration images across every reference site (compose, CI workflow,
  integration conftest, runtime env-fallbacks, runner script, and the
  meshtastic bridge example) in a single PR, so future bumps can no
  longer drift apart. The drift-guard test reads the compose source of
  truth at test time — no per-bump test edits — and now covers
  meshtasticd. Drifted meshtasticd sites were realigned to the
  compose-pinned `2.7.26`, and the CI workflow now pins the meshtasticd
  digest (matching compose) for deterministic runs. Renovate's
  `config:best-practices`-inherited `ignorePaths` (which skips
  `**/tests/**` and `**/examples/**`) is overridden to empty so the
  custom managers can reach the conftest and example-config reference
  sites — without it, the first synapse bump updated four of five sites
  and the drift guard correctly failed the PR.

## Matrix E2EE Identity

- **Own-device Matrix cross-signing lifecycle.** MEDRE now uses the currently
  pinned `mindroom-nio` cross-signing surface through a dedicated identity
  policy component. The policy verifies the server-visible master →
  self-signing → current-device chain, repairs only the current-device
  self-signature when the persisted identity matches, refuses automatic
  master/self-signing rotation on mismatches, and exposes secret-free
  diagnostics for provider/local/server/chain/recovery state.
- **Authenticated E2EE bootstrap and explicit recovery.** `medre adapter
matrix auth login --adapter-id <id>` prepares the selected adapter's exact
  runtime E2EE store and verifies cross-signing before credentials are
  persisted. Passwords remain transient. `--reset-cross-signing` is an
  explicitly destructive, fresh-password-authenticated recovery path; normal
  runtime startup cannot bootstrap or rotate account cross-signing identity.
- **Bounded Matrix auth HTTP operations.** Login, whoami verification, and
  logout now use the same explicit 30-second request timeout so cross-signing
  failure cleanup cannot hang indefinitely on an unresponsive homeserver.
- **Runtime identity reconciliation.** E2EE startup verifies existing
  cross-signing state and may safely repair a missing current-device
  signature. Missing/mismatched identity material is reported without
  downgrading encryption or rotating identity. Session/adapter diagnostics now
  expose cross-signing status without keys, signatures, passwords, tokens,
  sidecar contents, or crypto objects.
- **Peer-device trust policy clarified.** Cross-signing MEDRE's own device is
  separate from trusting other Matrix devices. E2EE sends intentionally retain
  `ignore_unverified_devices=True` for bot compatibility until a dedicated
  peer-device verification policy is introduced. Operator, security,
  transport-profile, limitations, install, and live-validation docs now match
  that behavior.
- **Docker cross-signing postcondition coverage.** The Synapse E2EE harness now
  includes a real-SDK identity test that performs password-authenticated
  cross-signing bootstrap, closes the client, reopens the same crypto store,
  and verifies the persisted server-visible chain without a password. The
  evidence remains Docker-local and does not claim federation or peer-device
  trust validation.

## Consolidated Fragment Era (150–185)

User-visible history of the numbered change fragments 150–185, consolidated
from the individual fragment files.

- **150 — Durable ingress storage foundation.** `LIVE`/`RECOVERED`/`HISTORY`
  provenance semantics for inbound events; atomic
  canonical-event/native-ref/work admission; corrupt provenance/work rejected
  at duplicate admission.
- **151 — Recoverable durable ingress worker.** Lease-based crash reclaim for
  pending ingress work; pipeline path for already-admitted events;
  protocol-provenance admission wired into adapter runtime context; per-item
  immediate claim before sequential processing.
- **152 — Matrix Classic Sync checkpoint ownership.** Runtime-managed Matrix
  adapters move to mindroom-nio Classic `sync_forever()` with bounded SDK
  retries and MEDRE-owned outer supervision; limited-timeline recovery with
  application-owned checkpoints and `LIVE`/`RECOVERED`/`HISTORY` admission;
  MEDRE storage persists the Matrix cursor and recovery-abandonment evidence
  before nio ack; failed durable admissions rejected at nio's boundary;
  durable callback trio enabled only when runtime storage is available.
- **153 — Matrix recovery failure evidence.** Durable-ingress worker counters
  exposed in runtime diagnostics; recovery-abandonment causes preserved with
  committed Matrix checkpoint; Matrix room IDs hidden from operator
  diagnostics while retained in internal checkpoint metadata; worker startup
  deferred until adapter startup completes.
- **154 — Adapter SDK parity.** LXMF outbound retains the
  `RNS.Destination` from `LXMRouter.register_delivery_identity()` as
  `LXMessage.source`; real-session startup fails explicitly when the local
  delivery identity cannot register; Reticulum pinned explicitly in the LXMF
  extra; LXMF stamp cost routed through `register_delivery_identity()` and
  validated to `0..254`; MeshCore reconnect uses `auto_reconnect=False` with
  no duplicate `send_appstart()` after factory connect and diagnostics read
  SDK `self_info`; propagated LXMF delivery requires an explicit
  outbound propagation-node destination hash; the owned `LXMRouter` is
  quiesced on stop/reconnect with its `atexit` callback unregistered and
  prior signal handlers preserved.
- **155 — MMRelay behavior reference.** MMRelay formalized as a non-runtime
  behavioral reference with executable MEDRE requirements (authenticated
  device discovery, E2EE peer-device rotation, bounded missing-room-key
  recovery, stale radio callbacks, SDK connection health, shutdown ordering,
  native reply construction); Matrix Megolm recovery retries detached from
  nio sync callbacks and cancelled at shutdown; permanent Matrix errcodes
  classified without retry; Meshtastic client replacement serialized with
  reader-thread callback validation and connection-generation revalidation.
- **156 — Transport realism.** Explicit transport test layers for exact-SDK
  local integration and soak endurance; LXMF real-session stop/restart
  releases router-owned Reticulum destinations and announce handlers;
  deterministic MeshCore TCP and process-isolated RNS/LXMRouter local
  integration; opt-in Meshtastic hardware lifecycle soak; CI gates for
  LXMF/MeshCore deterministic local integration plus manual soak jobs.
- **157 — Core reliability.** Canonical events durably admitted before
  routing/delivery; admitted work deferred (not failed) when capacity or
  shutdown prevents outbox transfer, so operational deferrals stay pending
  without consuming the poison-work retry budget; bounded ingress/fan-out
  concurrency and bounded shutdown grace for the active durable-ingress row;
  task-local structured correlation across
  ingress/plans/targets/attempts/receipts/replay; replay rendering
  reconstructed from persisted historical rendering context; durable same-run
  replay idempotency for non-empty replay run IDs;
  `confirmation_level` separates receipt lifecycle status from transport
  proof strength (`local_queue`, `local_transport`, `remote_service`);
  built-in adapters advertise deterministic thread-capability fallback;
  Prometheus text export for bounded aggregate numeric/boolean diagnostics.
  Storage compatibility: the `delivery_receipts.confirmation_level` column
  was added without a schema-version bump — existing prerelease databases
  intentionally fail required-column validation; there is no in-place
  migration (see the prerelease reset workflow).
- **158 — Matrix event normalization.** Versioned `native.matrix` namespace
  for sender/room/event/timestamp/relation/relay/media/encryption provenance;
  Matrix edits/redactions/threads/media normalized to transport-neutral
  kinds; Matrix media and redaction classes registered at the session
  boundary with safe-only decryption provenance (no key/session material);
  MMRelay compatibility fields isolated under `native.interop.mmrelay`;
  standalone JSON Schema published for Matrix-native metadata; Matrix
  reply/edit/thread/redaction takes precedence over MMRelay emote-reaction
  markers when both are present.
- **159 — Prerelease contract consolidation.** Canonical native metadata
  standardized per transport under `native.matrix`/`native.meshtastic`/
  `native.meshcore`/`native.lxmf`, with MMRelay kept under
  `native.interop.mmrelay`; `channel_room_map` is structured-only with a
  required `!` Matrix room ID and optional per-entry origin labels (bare IDs
  and `#` aliases rejected); the canonical event envelope is closed and
  versioned (extensions via `payload`, `metadata.custom`, and versioned
  namespaces); removed the canonical-event shape-conversion registry, the
  alternate replay render hook, the inline live-ingress fallback, the flat
  smoke-report reader, and the mixed real-adapter example config; runtime
  live adapter ingress now requires durable storage; positive-integer
  schema-version identifiers enforced; embedded Matrix/LXMF MEDRE relay
  envelopes remain current-version-only.
- **160 — Adapter lifecycle doc reconciliation.** `docs/spec/adapter-runtime.md`
  realigned to the 8-state `AdapterState` enum (`INITIALIZING`, `READY`,
  `DEGRADED`, `BACKPRESSURED`, `DISCONNECTED`, `STOPPING`, `FAILED`,
  `STOPPED`) with `VALID_TRANSITIONS` transcribed; stale `RUNNING`/
  `DRAINING` references updated across ops/dev docs. Documentation only.
- **164 — Atomic queued delivery finalization.** Delayed queue-backed sends
  finalized in one storage transaction coupling the outbound native-message
  reference, the immutable `sent` receipt, and the outbox attempt → `sent`
  transition; outbox ID/attempt/non-terminal state revalidated inside the
  transaction; conflicting native identities mapping to a different
  canonical event rejected.
- **165 — Local-integration evidence.** MeshCore real-SDK TCP and LXMF
  process-isolated local-integration gates recorded as executed; capability
  status vocabulary distinguishes deterministic real-SDK local integration
  from Docker and from external live/hardware validation.
- **166 — Deterministic SQLite execution path.** `SQLiteStorage` no longer
  switches to `aiosqlite` at import time; all installs use stdlib `sqlite3`
  behind MEDRE's single-worker executor; durable ingress, outbox, generic
  read/write, read-only open, and queued-delivery finalization collapsed onto
  the existing synchronous transaction authorities; the public storage API
  stays async with WAL/busy-timeout/foreign-key enforcement retained.
- **167 — Retry outbox lifecycle authority.** Retry-worker abandonment,
  backoff, exhaustion, dead-letter, and success transitions routed through
  `DeliveryLifecycleService`; live and retry delivery share one
  state-transition authority; polling, claim orchestration, capacity,
  counters, and operational events stay in `RetryWorker`.
- **168 — Reverse relation traversal.** Storage read API for listing unique
  source event IDs whose relations target a canonical event, ordered by
  first relation insertion; the existing `target_event_id` SQLite index
  documented as the authority; reverse-traversal primitive for later
  conversation-graph repair.
- **169 — Retry lifecycle authority finish.** Retry-attempt receipt
  correlation, failure classification, retry scheduling, and dead-letter
  decisions moved behind `DeliveryLifecycleService`; `RetryWorker` direct
  storage narrowed to claim/read; retry evidence correlated by exact
  `outbox_id`/target/attempt/receipt lineage; missing or malformed retry
  evidence treated as invariant violations, with malformed failed-receipt
  taxonomy repaired as terminal `adapter_permanent`; persisted retry
  timestamps reused and duplicate resend prevented when queued/sent evidence
  exists; non-retryable retry failures dead-letter immediately with
  `outbox_id` on the dead-letter receipt; next-attempt evidence reconciled
  after lease reclaim; lifecycle outcomes (`reconciled`, `suppressed`,
  `retry_wait`, `accepted`, `dead_lettered`) reported via runtime
  counters/events; the durable outbox clarified as an operational work queue
  with receipts as immutable evidence.
- **170 — Retry startup recovery visibility.** Retry snapshots expose
  `abandoned` and `previous_run_in_progress`; `retry_unfinished_work_detected`
  event emitted at startup (diagnostic only); startup outbox-count read is
  non-fatal and bounded by a 5-second preflight; `RetryWorker.start()`/
  `stop()` serialized by one lifecycle lock; boolean row counts rejected.
- **171 — Conversation projection convergence.** New mutable rebuildable
  `conversation_membership` current-state view (canonical events, relation
  rows, and native-message refs remain immutable evidence); deterministic
  reverse lookup for native relation targets; transitive recompute with
  serialized projection repairs and bounded idempotent rebuild at startup
  when dirty, interrupted, or at an older revision; clean shutdown records a
  marker to skip the redundant rebuild; routing/rendering consume an
  in-memory copy overlaid with the current projection; cycle behavior uses
  the lexicographically-smallest cycle event ID as the projection root;
  prerelease SQLite schema stays at `1` — databases lacking the new shape
  are rejected with no compatibility path; required SQL CHECKs validated
  from parsed clauses; interrupted rebuilds resume from the persisted
  cursor.
- **172 — Delivery coordinator decomposition.** Per-target delivery
  orchestration extracted from `PipelineRunner` into an orchestration-only
  `DeliveryCoordinator`; preflight order and bounded ordered fan-out
  preserved; the delivery-capacity slot is released on every exit path
  (cancellation during outbox creation no longer leaks capacity;
  outbox-finalization failure no longer strands capacity or a stale shutdown
  identity); architecture guards prove no direct storage mutations.
- **173 — Delivery coordinator review fixes.** Capacity-controller
  replacement routed through `PipelineRunner.set_capacity_controller()`;
  failed adapter/renderer outcomes retain the persisted `DeliveryReceipt`
  from `TargetDeliveryService`; coordinator/outbox finalization use the
  exact stored row including the storage-assigned receipt sequence when
  read-back is available; the runner no longer mirrors delivery capacity
  state; run-session capacity-rejection setup fails cleanly without a
  pipeline runner and guidance describes the durable suppression receipt.
- **185 — Adapter SDK contract pin authority.** Installed-SDK contract tests
  derive expected package versions from the exact `pyproject.toml` pins
  instead of duplicating literals; contract probes bind only MEDRE-consumed
  call shapes (no freezing of unrelated defaults, enums, or limits);
  MeshCore APP_START coverage accepts additive SDK handshake arguments while
  requiring exactly one handshake on initial connection and one on
  SDK-owned reconnect; LXMF lifecycle coverage checks callable/observable
  ownership surfaces rather than source-code substrings; a structural guard
  keeps the LXMF/Meshtastic/MeshCore SDK extras exact-pinned without
  freezing their version numbers in a second authority.
