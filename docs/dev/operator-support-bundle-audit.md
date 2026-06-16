# Operator Support Bundle Audit

Date: 2026-06-16
Branch: operator-support-bundle

## Summary

MEDRE already ships most of the machinery a `medre diagnostics bundle`
command needs. The centre of gravity is
`src/medre/runtime/evidence/_bundle.py::collect_evidence_bundle()`, a
252-line orchestrator that assembles six JSON-safe sections
(`config_summary`, `route_validation`, `diagnostics_snapshot`,
`live_health`, `storage`, `recovery`) plus seven hoisted top-level
surfaces (`adapter_status`, `shutdown_evidence`, `convergence_summary`,
`orphan_report`, `lifecycle_convergence_report`, `recovery_summary`,
`recovery_ledger`). It already supports two mutually exclusive collection
modes — config-path mode (loads YAML, builds runtime, takes live health
optionally) and storage-path direct mode (opens SQLite read-only, no
config needed) — and already emits `schema_version`, `medre_version`,
`config_source`, `evidence_tier`, `runtime_started`, `errors`, and a
fixed `limitations` array. The existing `medre evidence --storage-path
<path> --json` CLI surfaces only the storage-path branch; the
config-path branch is reachable only via the Python API or via
`medre inspect event <id> --evidence` (which forces an `event_id`).

What is reusable, then, is essentially the entire collector. What is
missing for a true "operator support bundle" command is (a) a CLI
surface that exposes the config-path branch without requiring an
`event_id` or `--storage-path`, (b) a multi-file output format (the
current collector returns a dict; the CLI does `print(json.dumps(...))`
and operators improvise via shell redirection), and (c) a small amount
of glue to attach an existing redacted config snapshot, route plan, and
runtime metadata that today live in sibling commands.

The redaction story is broadly solid but fragmented. Five separate
secret-detection surfaces exist (YAML key-name set, env-var field-name
regex, `sanitize_for_log` key patterns, `sanitize_error` in-string
token regex, `EnvProvenance.redacted_items()` field-name heuristic)
with overlapping but not identical coverage. All evidence*bundle paths
route error strings through `sanitize_error` and the collector never
introspects adapter config internals (it exposes only the wrapper
fields `adapter_id` / `transport` / `enabled` / `adapter_kind`). The
existing redaction tests are extensive (sanitiser hardening, snapshot
stress, storage-only secret-leakage, env-provenance masking) and a new
bundle command can lean on them. The gaps that remain are mostly about
\_coverage of new surfaces* (file paths, ZIP entries, manifest) rather
than holes in the existing collector.

## Methodology

Read-only audit. No tests were run, no source files were modified.

1. Read the mandatory entry points
   (`docs/dev/testing.md`, `docs/dev/TESTING_GUIDE.md`, `AGENTS.md`).
2. Mapped the full CLI command surface by reading
   `src/medre/cli/main.py` end-to-end and each command module
   (`diagnostics_commands.py`, `evidence_commands.py`,
   `config_commands.py`, `route_commands.py`, `inspect_commands.py`,
   `recover_commands.py`, `trace_commands.py`, `storage_commands.py`).
3. Read every file under `src/medre/runtime/evidence/` (`_bundle.py`,
   `_config_sections.py`, `_diagnostics_sections.py`,
   `_storage_sections.py`, `_recovery_sections.py`, `_helpers.py`,
   `__init__.py`) and the upstream collector
   `src/medre/core/evidence/collector.py`.
4. Read the config stack: `config/loader.py`, `config/env.py`,
   `config/_yaml.py`, `config/errors.py`, `config/model.py`,
   `config/paths.py`.
5. Read the redaction stack:
   `core/observability/sanitization.py` and the redaction helpers in
   `runtime/docker_bridge_artifacts.py` (`redact_config_snapshot`,
   `_redact_strings`, `_yaml_escape_string`, `write_summary`).
6. Read the route-plan model (`runtime/route_plan.py`) and the
   runtime-snapshot contract (`runtime/snapshot.py` docstring).
7. Inventoried existing tests for diagnostics redaction
   (`test_cli_diagnostics_commands.py::TestSecretRedaction`,
   `test_cli_diagnostics_workflows.py::test_diagnostics_no_secrets`,
   `test_sanitizer_hardening.py`, `test_bounded_evidence_safety.py`,
   `test_snapshot_stress.py`, `test_storage_only_evidence.py`,
   `test_config_env.py`).
8. Inventoried existing audits that already cover overlapping ground
   (`docs/dev/operator-surface-audit.md`,
   `docs/dev/runtime-evidence-completeness-audit.md`) and operator
   docs (`docs/ops/diagnostics-and-evidence.md`,
   `docs/ops/troubleshooting.md`, `docs/ops/running-medre.md`).
9. Searched for any existing ZIP/tarball writer
   (`grep zipfile|ZipFile|tarfile|\.zip` under `src/`) — none found.

Throughout, "support bundle" means the proposed offline, redacted,
operator-shareable artifact; "evidence bundle" means the existing
`collect_evidence_bundle()` dict shape.

## Findings

### [F-001] `collect_evidence_bundle()` is the primary reusable surface

- **Category**: reusable code
- **Location**: `src/medre/runtime/evidence/_bundle.py:43-252`
- **Current state**: Async function
  `collect_evidence_bundle(config_path=None, *, event_id=None,
replay_run_id=None, include_refresh_health=False, storage_path=None,
now_fn=None) -> dict[str, Any]`. Returns a JSON-safe dict with
  `schema_version=1`, `command="evidence"`, `status` (`passed` /
  `partial` / `error`), and six nested sections under `sections`.
  Two mutually exclusive modes: storage-path direct (no config) and
  config-path (loads YAML, optionally refreshes live health).
  Already routes every error string through
  `sanitize_error()` (`_helpers.py:82-90`). Already hoists seven
  derived surfaces to the top level for operator visibility.
- **Expected state**: Reused verbatim by `medre diagnostics bundle`.
  The new command adds CLI plumbing (argparse, output writer) and
  possibly a manifest section; the collector itself does not need to
  change.
- **Recommendation**: Do not fork or wrap. Call
  `collect_evidence_bundle()` directly from the new CLI handler.
  Reuse its `limitations`, `medre_version`, `schema_version`, and
  `collected_at` / `generated_at` timestamps verbatim.

### [F-002] Existing `medre evidence` CLI exposes only storage-path mode

- **Category**: duplicate command behavior
- **Location**: `src/medre/cli/main.py:164-188` (parser),
  `src/medre/cli/evidence_commands.py:13-32` (handler)
- **Current state**: `medre evidence --storage-path <path> [--event ID]
[--replay-run RUN] [--json]`. The parser marks `--storage-path`
  `required=True`. The handler hard-codes
  `collect_evidence_bundle(None, ..., storage_path=storage_path)` —
  it never passes a config path, so the config-backed collection
  branch (live health, diagnostics snapshot, config summary) is
  unreachable from the CLI.
- **Expected state**: `medre diagnostics bundle` covers the
  config-backed branch the existing command omits, and explicitly
  documents the overlap. The operator surface audit
  (`docs/dev/operator-surface-audit.md` §1.3) already lists `medre
evidence` under "Evidence Bundle"; adding a second bundle-producing
  command without a clear scope split will create ambiguity.
- **Recommendation**: Pick one of two scopes for the new command and
  document it in `docs/dev/operator-surface-audit.md` §1.3 and
  `docs/ops/diagnostics-and-evidence.md`:

  - **Option A (recommended)**: `medre diagnostics bundle` becomes the
    single bundle command. It accepts either `--config` or
    `--storage-path` (mirroring `collect_evidence_bundle`'s two
    modes), adds an output-path flag (`--out PATH`), and produces a
    multi-file artifact. `medre evidence` is kept as a thin
    compatibility alias or deprecated.
  - **Option B**: `medre diagnostics bundle` is config-path-only and
    complements `medre evidence` (which stays storage-path-only).
    Document the split explicitly in both helps.

  Either way, the `medre evidence` help text
  (`src/medre/cli/main.py:164-167`: "Specialized support bundle,
  usually inspect event --evidence") already signals it is not the
  primary path.

### [F-003] `medre diagnostics` is a flat command, not a subcommand group

- **Category**: reusable code
- **Location**: `src/medre/cli/main.py:71-82`
- **Current state**:
  ```python
  diag_p = sub.add_parser("diagnostics", help="...")
  diag_p.add_argument("--config", ...)
  diag_p.add_argument("--refresh-health", ...)
  ```
  No `add_subparsers(dest="diagnostics_command")`. Dispatch in
  `main.py:472-478` checks `getattr(args, "refresh_health", False)`.
- **Expected state**: To add `medre diagnostics bundle`, the parser
  must be restructured into a subcommand group
  (`diagnostics snapshot` for the current behaviour, `diagnostics
bundle` for the new behaviour), **or** a sibling top-level command
  must be added.
- **Recommendation**: Restructure `diagnostics` into a subcommand
  group. Preserve the current default by making the bare `medre
diagnostics` (no subcommand) equivalent to `medre diagnostics
snapshot --config ...`. Mirror the pattern already used by
  `config`, `routes`, `inspect`, `trace`, `storage`
  (`main.py:55-56`, `84-85`, `191-195`, `276-280`, `391-392`). This
  is a breaking CLI change — call it out in
  `docs/changes/unreleased.md` and update help-hint tests
  (`tests/test_cli_command_help_hints.py`).

### [F-004] No ZIP / tarball writer exists in the source tree

- **Category**: missing surface (intentionally absent, not a bug)
- **Location**: `grep -r 'zipfile\|ZipFile\|tarfile\|\.zip' src/`
  returns zero matches.
- **Current state**: Every existing "bundle-like" output is either a
  single JSON blob printed to stdout (`evidence_commands.py`,
  `diagnostics_commands.py`) or a directory of artifact files plus a
  `summary.json` (`runtime/docker_bridge_artifacts.py:402-414`,
  `write_summary()`). The docker-bridge artifact pattern is the
  closest precedent: `create_run_directory()` makes a timestamped
  directory, individual artifact files are written by the docker
  test, and `write_summary()` writes `summary.json` with
  `json.dumps(summary, indent=2, sort_keys=True, default=str)`.
- **Expected state**: A support bundle is typically a single
  portable file (`.zip` or `.tar.gz`) operators can attach to an
  issue. Stdlib `zipfile.ZipFile` is sufficient; no new dependency
  needed.
- **Recommendation**: Use `zipfile.ZipFile(path, "w",
zipfile.ZIP_DEFLATED)` and write each section as a separate JSON
  member (`config_summary.json`, `route_plan.json`, `snapshot.json`,
  `storage.json`, `recovery.json`, `manifest.json`). Add a
  top-level `manifest.json` listing member names, byte sizes, and
  SHA-256 hashes so recipients can verify integrity. Keep the
  directory-of-files approach from
  `runtime/docker_bridge_artifacts.py` as a `--format dir|zip`
  option if operators prefer inspectable-on-disk output. Do **not**
  add tar/gzip — zip is universally openable on operator
  workstations, and stdlib `zipfile` is enough.

### [F-005] Redaction surface is fragmented across five overlapping implementations

- **Category**: unsafe surface (latent, not currently exploited)
- **Location**:
  - `src/medre/config/_yaml.py:186-197` — `_SECRET_KEY_NAMES`
    frozenset (8 names: access_token, password, secret, api_key,
    apikey, token, private_key, client_secret). Used only by
    `_redact_key()` for YAML duplicate-key error messages.
  - `src/medre/config/env.py:201-204` — `_SECRET_FIELD_RE` regex
    (`TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL|BLE|IDENTITY`,
    case-insensitive). Used by `_is_secret_field()` for env-var
    provenance redaction.
  - `src/medre/core/observability/sanitization.py:26-42` —
    `_SECRET_KEY_PATTERNS` tuple of 12 anchored regexes (password,
    secret*, private_key*, access_token, auth_token, api_key,
    credential(s), session_secret, encryption_key, device_key,
    signing_key, identity_key). Used by `sanitize_for_log()` to
    **drop** matching keys from dicts.
  - `src/medre/core/observability/sanitization.py:100-112` —
    `_TOKEN_RE` in-string regex with 10 alternations (syt\_, MDAx,
    40+ char base64, sk-, api_key=..., access_token=..., token=...,
    password=..., secret=..., credentials=...). Used by
    `sanitize_error()` to redact token-like substrings inside
    arbitrary strings.
  - `src/medre/runtime/docker_bridge_artifacts.py:358-372` —
    `_redact_strings()` recursive walker that applies
    `sanitize_error()` to every string value in a dict. Used only
    by docker-bridge summary building.
- **Current state**: Each surface covers a slightly different key
  set. For example, `_SECRET_KEY_NAMES` includes `apikey` (one word)
  but `_SECRET_FIELD_RE` does not; `_SECRET_FIELD_RE` includes
  `BLE` and `IDENTITY` (needed for MeshCore and LXMF respectively)
  but `_SECRET_KEY_NAMES` does not. The collector never introspects
  adapter config internals, so the fragmentation has not produced a
  known leak — but a new bundle command that writes a redacted
  `config.yaml` artifact (recommended in F-009) would need to pick
  one surface and accept that the others disagree.
- **Expected state**: A single source of truth for "what is a
  secret key" that all five callers use. The most complete existing
  surface is `_SECRET_KEY_PATTERNS` in `sanitization.py` (12
  patterns, anchored, used by the public `sanitize_for_log` API).
- **Recommendation**: Do not unify as part of this change — it is a
  separate refactor with its own test surface. Instead, the new
  bundle command should use `sanitize_for_log()` (the public API)
  for any config-snapshot artifact and document in
  `docs/dev/operator-support-bundle-audit.md` (this file) that the
  unification is deferred. If a bundle artifact is found to leak via
  a key-name mismatch, the fix is to extend
  `_SECRET_KEY_PATTERNS` (the most-complete surface), not to add a
  sixth pattern.

### [F-006] `EnvProvenance.redacted_items()` uses field-name heuristic only

- **Category**: missing redaction (latent)
- **Location**: `src/medre/config/env.py:263-293`
- **Current state**: For each recorded env var, the field-name
  segment (after the last `__` for adapter vars, or the whole name
  for core vars) is matched against `_SECRET_FIELD_RE`. If it
  matches, the value is replaced with `***REDACTED***`. If a secret
  value is bound to a non-secret-named env var (e.g.
  `MEDRE_ADAPTER__MATRIX_MAIN__HOMERVER=secret_token`), the value
  is **not** redacted by this layer. The evidence bundle exposes
  only the names (`env_overrides_applied: list[str]` in
  `_config_sections.py:68-71`), so no value reaches the bundle
  through this path today.
- **Expected state**: A bundle that includes a `redacted_config.yaml`
  artifact (F-009) or operator-captured env-var dumps should run
  every value through `sanitize_error()` as a defence-in-depth.
- **Recommendation**: Continue to expose env-override **names only**
  in the bundle (`env_overrides_applied`). If the bundle ever adds
  an env-var dump artifact, route every value through
  `sanitize_error()` in addition to the field-name check, and add a
  test in `tests/test_sanitizer_hardening.py` that proves a
  secret-looking value in a benign-named env var is caught by the
  in-string regex.

### [F-007] `medre routes plan --json` is a clean reusable JSON artifact

- **Category**: reusable code
- **Location**: `src/medre/cli/route_commands.py:323-350`,
  `src/medre/runtime/route_plan.py:166-323`
- **Current state**: `_routes_plan(config_path, as_json=False)`
  loads config, calls `build_route_plan(config)`, and either renders
  human-readable text or prints
  `json.dumps(asdict(plan), indent=2, sort_keys=True)`. The
  `RoutePlan` dataclass (`route_plan.py:151-158`) is frozen and
  contains only `adapters: list[AdapterSummary]`, `routes:
list[RoutePlanEntry]`, `total_legs: int`, `loops: list[str]`.
  Every nested dataclass (`AdapterSummary`, `RoutePlanLeg`,
  `RoutePlanEntry`) is also frozen and JSON-safe (only `str`, `int`,
  `bool`, `list`, `None`). No secrets — origin labels and adapter
  IDs only.
- **Expected state**: Reusable as a bundle member without
  modification.
- **Recommendation**: Add `build_route_plan(config)` output as a
  bundle member named `route_plan.json`. Call it from inside the
  bundle collector (or the CLI handler, before zipping). The plan
  is offline-only and cheap; no lifecycle impact.

### [F-008] `ConfigSource` enum and `MedrePaths.to_diagnostics()` are JSON-safe and reusable

- **Category**: reusable code
- **Location**: `src/medre/config/loader.py:49-56` (ConfigSource),
  `src/medre/config/paths.py:208-223` (to_diagnostics)
- **Current state**: `ConfigSource` is a str enum with five values
  (`EXPLICIT`, `MEDRE_CONFIG`, `MEDRE_HOME`, `XDG`, `LOCAL`);
  `.value` is JSON-safe. `MedrePaths.to_diagnostics()` returns a
  dict of eight string fields (`config_dir`, `config_file`,
  `state_dir`, `data_dir`, `cache_dir`, `log_dir`,
  `database_path`, `adapter_state_root`) — all filesystem paths,
  no secrets. The evidence bundle already uses both via
  `_config_sections.py:: _collect_config_summary()` (lines 84-94),
  exposing `config_source` at the top level and `paths` inside
  `config_summary`.
- **Expected state**: Reused verbatim.
- **Recommendation**: No change. Bundle should continue to expose
  `config_source` at the top level and the full `paths` dict inside
  `config_summary`. Operators need filesystem paths to interpret
  storage and adapter state locations.

### [F-009] No redacted `config.yaml` snapshot artifact exists in the bundle

- **Category**: missing surface
- **Location**: Bundle sections `_collect_config_summary()`
  (`runtime/evidence/_config_sections.py:19-96`) deliberately
  exposes only adapter wrapper metadata (transport, adapter_id,
  enabled, adapter_kind) and never adapter config internals.
- **Current state**: The bundle contains a **summary** of the config
  (counts, IDs, kinds, paths, limits, route IDs) but not the config
  itself. Operators who attach a bundle to a support issue often
  need the full config to reproduce; today they must remember to
  attach `config.yaml` separately and redact it manually. The
  docker-bridge artifact pipeline already solves this:
  `runtime/docker_bridge_artifacts.py::redact_config_snapshot()`
  (line 265-271) wraps `sanitize_for_log()` to drop secret keys
  from a config dict, and `_yaml_escape_string()` (line 482+) and
  `_format_yaml_key()` (referenced at line 485-488 in the
  changelog) produce round-trippable YAML.
- **Expected state**: A `redacted_config.yaml` (or
  `redacted_config.json`) member in the bundle, produced by
  applying `sanitize_for_log()` to the parsed config dict and
  re-serialising.
- **Recommendation**: Add a new section or bundle member that
  reuses `redact_config_snapshot()` from
  `runtime/docker_bridge_artifacts.py`. Do **not** hand-roll a new
  redactor. Cover the new surface with a test in
  `tests/test_storage_only_evidence.py::TestStorageOnlyNoSecretLeakage`
  style that asserts `access_token`, `password`, `secret`,
  `api_key`, `credentials`, and `ble_address` keys are absent from
  the serialised output.

### [F-010] `_get_version()` is duplicated across two modules

- **Category**: duplicate command behavior
- **Location**: `src/medre/cli/main.py:15-20` and
  `src/medre/runtime/evidence/_helpers.py:51-56`
- **Current state**: Two identical implementations:
  ```python
  def _get_version() -> str:
      try:
          return importlib.metadata.version("medre")
      except importlib.metadata.PackageNotFoundError:
          return "0.1.0"
  ```
  The evidence bundle already embeds the version as
  `medre_version` at the top level (`_bundle.py:243`). `medre
diagnostics` (no bundle) does not emit a version field today.
- **Expected state**: One shared helper.
- **Recommendation**: Extract a single helper to
  `src/medre/cli/_version.py` (or reuse
  `runtime/evidence/_helpers.py::_get_version` directly) and have
  both `main.py::_version()` and the bundle command import it.
  Minor cleanup, defer if it interrupts the change.

### [F-011] `medre recover` runbook is not currently a bundle section

- **Category**: reusable code
- **Location**: `src/medre/cli/recover_commands.py` (341 lines),
  `src/medre/core/recovery/builder.py`,
  `src/medre/core/recovery/classification.py`
- **Current state**: The bundle already includes a `recovery`
  section (`runtime/evidence/_recovery_sections.py:29-149`) that
  builds a snapshot-diagnostics recovery ledger and summary from
  the outbox. However, the per-event **recovery runbook** that
  `medre recover --event <id>` produces (with
  `failure_classification`, `recommended_commands`, `commands`,
  `warnings`, optional `dry_run` preview) is not in the bundle.
  Operators investigating a specific event must run `medre recover`
  separately.
- **Expected state**: When the bundle is collected with an
  `event_id`, the per-event recovery runbook should be embedded as
  a `recovery_runbook` field in the storage section or as a new
  `incident_runbook` top-level field.
- **Recommendation**: Optional. The current `incident_summary`
  field in the storage section (`_storage_sections.py:291-310`)
  already includes `recommended_commands` and `commands` for the
  event. Extending it to include the full runbook is additive. Defer
  unless operator feedback asks for it.

### [F-012] No "what to attach to a support issue" operator guidance exists

- **Category**: missing test (of operator docs)
- **Location**: `docs/ops/troubleshooting.md` (820 lines),
  `docs/ops/diagnostics-and-evidence.md:37-87` ("Quick Bundle
  Collection")
- **Current state**: `diagnostics-and-evidence.md` has a "Quick
  Bundle Collection" section that shows operators how to redirect
  `medre smoke --json` and `medre evidence --json` to files via
  shell redirection. `troubleshooting.md` has no explicit "when
  filing a bug, attach X" section. No operator doc mentions a
  single-command bundle.
- **Expected state**: A dedicated "Filing a Support Issue" section
  in `docs/ops/troubleshooting.md` (or a new
  `docs/ops/support-bundle.md`) that lists exactly what to run and
  what to attach.
- **Recommendation**: Add the section as part of this change. Point
  operators at `medre diagnostics bundle --config <path> --out
medre-bundle.zip` (or the equivalent final command shape) and
  explain what is inside, what is redacted, and what is not
  (filesystem paths, adapter IDs, route IDs, log levels — all
  included; tokens, passwords, env-var values — all redacted).

### [F-013] Bundle CLI exit-code contract is partially defined

- **Category**: missing surface
- **Location**: `src/medre/cli/evidence_commands.py:72-73`,
  `src/medre/cli/exit_codes.py`
- **Current state**: `medre evidence` exits 0 for `passed` or
  `partial`, `EXIT_CONFIG (2)` for outright config-load failure
  (status `"error"`). `medre diagnostics` uses `EXIT_CONFIG (2)`,
  `EXIT_BUILD (3)`, `EXIT_STARTUP (4)` to distinguish failure
  phases. `EXIT_NOT_FOUND (5)` is defined but unused by evidence.
  No exit code is reserved for "bundle wrote but some sections
  errored".
- **Expected state**: A bundle command that writes a file should
  distinguish "wrote a healthy bundle" from "wrote a partial
  bundle" from "failed to write anything".
- **Recommendation**: Use the existing convention: exit 0 if the
  bundle was written (regardless of section status — operators
  inspect the JSON `status` field), exit `EXIT_CONFIG (2)` if
  config could not be loaded, exit `EXIT_BUILD (3)` if the runtime
  could not be built for the diagnostics snapshot, exit
  `EXIT_STARTUP (4)` if `--refresh-health` was requested and
  startup failed. Do not introduce a new code.

### [F-014] Global convergence queries are bounded at 10,000 rows

- **Category**: intentionally deferred
- **Location**:
  `src/medre/runtime/evidence/_storage_sections.py:25-26`,
  `:445-462`
- **Current state**: When the bundle is collected without an
  `event_id`, global convergence queries (`list_all_receipts`,
  `list_all_outbox_items`) are capped at
  `_GLOBAL_CONVERGENCE_LIMIT = 10_000` rows per table. If either
  hits the limit, the section status becomes `partial` and a
  `convergence_truncated_warning` string is embedded in the data.
- **Expected state**: Documented limitation; bundle should surface
  it clearly to operators receiving a partial bundle.
- **Recommendation**: No change. The truncation warning is already
  in `_LIMITATIONS` (`_helpers.py:43`) and surfaced both in the
  section data and at the top level. Document the 10K cap in the
  new "Filing a Support Issue" operator section so operators know
  to pass `--event <id>` for targeted investigation of large
  databases.

### [F-015] Existing diagnostics tests cover redaction but not bundle output

- **Category**: missing test
- **Location**: `tests/test_cli_diagnostics_commands.py:113-132`
  (`TestSecretRedaction`), `tests/test_cli_diagnostics_workflows.py`
  (`test_diagnostics_no_secrets`),
  `tests/test_storage_only_evidence.py:391-435`
  (`TestStorageOnlyNoSecretLeakage`)
- **Current state**: Existing tests assert that `medre diagnostics`,
  `medre config check`, `medre routes list`, and `medre routes
topology` outputs lack the substrings `tok`, `access_token`,
  `password`, `api_key`, `secret`. The storage-only evidence tests
  assert the same for `collect_evidence_bundle(storage_path=...)`.
  No test asserts redaction for a bundle written to a file or zip.
- **Expected state**: A test class that collects a bundle from a
  config containing known secret values (e.g. `access_token:
fake_tok_value`, `password: hunter2`, `ble_address: AA:BB:CC...`,
  `identity_path: /path/to/identity`), writes it via the new
  command, reads the output back, and asserts none of the secret
  substrings appear in any member.
- **Recommendation**: Add `tests/test_cli_diagnostics_bundle.py`
  (or extend `test_cli_diagnostics_commands.py` if under the 1,500
  line cap — currently 430 lines, so room exists). Cover: (1)
  config-path mode with secrets in config, (2) storage-path mode
  with secrets in receipts/outbox (already covered by
  `test_storage_only_evidence.py` but worth a CLI-level test), (3)
  ZIP output integrity (member list, manifest, SHA-256 digests), (4)
  exit codes per F-013.

### [F-016] Runtime snapshot is JSON-safe but large

- **Category**: reusable code
- **Location**: `src/medre/runtime/snapshot.py:1-120` (docstring
  contract)
- **Current state**: `build_runtime_snapshot(app, ...)` returns a
  dict with 17 sections (`schema_version`, `snapshot_at`,
  `snapshot_scope`, `accounting`, `adapters`, `capacity`,
  `diagnostics`, `health`, `identity`, `lifecycle`, `limits`,
  `outbox`, `persistence`, `replay`, `routes`, `startup`,
  `unstable`). The docstring at `snapshot.py:80-88` guarantees:
  deterministic key ordering, no SDK objects, no secrets (adapter
  configs never introspected), bounded size, graceful degradation.
  The evidence bundle already embeds this snapshot inside the
  `diagnostics_snapshot` and `live_health` sections
  (`_diagnostics_sections.py:243-294`, `:301-362`).
- **Expected state**: Reusable as-is.
- **Recommendation**: When the bundle command writes individual zip
  members, write the snapshot as its own member (`snapshot.json`)
  in addition to keeping it nested in `diagnostics_snapshot.data`
  inside the top-level `bundle.json`. Operators frequently want to
  grep the snapshot without parsing the full bundle envelope.

### [F-017] `medre storage status` output is not part of any bundle

- **Category**: reusable code
- **Location**: `src/medre/cli/storage_commands.py:20-95`
- **Current state**: `_storage_status(storage_path)` opens the DB
  read-only via a raw `sqlite3.connect("file:...?mode=ro", uri=True)`
  and reports schema version, expected version, and per-table
  missing-column validation. Output is human-readable text, not
  JSON.
- **Expected state**: The storage health information is valuable in
  a support bundle, especially when an operator reports storage
  corruption or a prerelease-schema mismatch.
- **Recommendation**: Optional. Refactor `_storage_status` to
  expose a structured `_storage_status_dict(storage_path) -> dict`
  helper that the bundle collector can call. The CLI command keeps
  its human-readable rendering by formatting the dict. Low priority
  — the evidence bundle's `storage` section already reports
  `db_exists`, `db_path`, `event_count`, `receipt_count`, and
  surfaces any read error as a partial status.

### [F-018] `MedreEnvConfig.to_dict()` exposes raw env-var values unredacted

- **Category**: unsafe surface (documented, not currently exploited)
- **Location**: `src/medre/config/env.py:821-827`
- **Current state**:
  ```python
  def to_dict(self) -> dict[str, str]:
      """Return dict of all set env vars (raw values, for diagnostics).
      Secret values are included unredacted — this is intended for
      programmatic use, not for logging."""
  ```
  The evidence bundle does **not** call `to_dict()` — it calls
  `provenance.redacted_items()` and exposes only the names
  (`_config_sections.py:68-71`). So no leak reaches the bundle
  today.
- **Expected state**: The bundle must never call `to_dict()` for
  any artifact that leaves the operator's machine.
- **Recommendation**: Add a module-level comment in the new bundle
  command warning against `MedreEnvConfig.to_dict()`. The
  existing `redacted_items()` is the only safe accessor. Consider
  deprecating `to_dict()` in a follow-up (rename to
  `to_unredacted_dict()` to make the unsafe shape obvious from
  call sites).

## Reusable Components Inventory

| Component                                      | Location                                                  | What it does                                                                       | Reuse for bundle?                                |
| ---------------------------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------- | ------------------------------------------------ |
| `collect_evidence_bundle()`                    | `runtime/evidence/_bundle.py:43-252`                      | Orchestrates 6 sections + 7 hoisted surfaces; config-path or storage-path mode     | Yes — call directly                              |
| `_collect_config_summary()`                    | `runtime/evidence/_config_sections.py:19-96`              | Adapter wrappers, routes, limits, paths, env-override names, storage backend       | Yes — embedded via collector                     |
| `_collect_route_validation()`                  | `runtime/evidence/_config_sections.py:104-157`            | Route count, errors, warnings, validity flag                                       | Yes — embedded via collector                     |
| `_collect_diagnostics_snapshot()`              | `runtime/evidence/_diagnostics_sections.py:243-294`       | Builds runtime, captures snapshot, derives adapter_status + shutdown_evidence      | Yes — embedded via collector                     |
| `_collect_live_health()`                       | `runtime/evidence/_diagnostics_sections.py:301-362`       | Starts runtime, refreshes health once, stops cleanly                               | Yes — opt-in via `include_refresh_health=True`   |
| `_collect_storage_section()`                   | `runtime/evidence/_storage_sections.py:563-624`           | Opens SQLite RO, runs counts + optional event lookup + convergence                 | Yes — embedded via collector                     |
| `_collect_recovery_section()`                  | `runtime/evidence/_recovery_sections.py:29-149`           | Builds snapshot-diagnostics recovery ledger + summary from outbox                  | Yes — embedded via collector                     |
| `build_runtime_snapshot()`                     | `runtime/snapshot.py`                                     | 17-section deterministic runtime state dict                                        | Yes — via `_collect_diagnostics_snapshot`        |
| `build_route_plan()`                           | `runtime/route_plan.py:166-323`                           | Offline per-leg expansion with origin-label provenance                             | Yes — add as bundle member `route_plan.json`     |
| `sanitize_error()`                             | `core/observability/sanitization.py:117-137`              | Strip token patterns + truncate to 512 chars                                       | Yes — already used by collector                  |
| `sanitize_for_log()`                           | `core/observability/sanitization.py:77-88`                | Drop secret keys + coerce values                                                   | Yes — use for any config-snapshot artifact       |
| `EnvProvenance.redacted_items()`               | `config/env.py:263-293`                                   | Env-var name + redacted value pairs                                                | Yes — already used by `_collect_config_summary`  |
| `MedrePaths.to_diagnostics()`                  | `config/paths.py:208-223`                                 | Eight filesystem-path strings                                                      | Yes — already used by `_collect_config_summary`  |
| `ConfigSource` enum                            | `config/loader.py:49-56`                                  | Five discovery sources                                                             | Yes — already used; `.value` is JSON-safe        |
| `redact_config_snapshot()`                     | `runtime/docker_bridge_artifacts.py:265-271`              | Wraps `sanitize_for_log()` for a config dict                                       | Yes — for F-009 redacted config artifact         |
| `_redact_strings()`                            | `runtime/docker_bridge_artifacts.py:358-372`              | Recursive `sanitize_error` over dict string values                                 | Yes — defence-in-depth for any summary dict      |
| `_yaml_escape_string()` / `_format_yaml_key()` | `runtime/docker_bridge_artifacts.py:482+`                 | Round-trippable YAML serialisation                                                 | Yes — if the redacted config is emitted as YAML  |
| `write_summary()`                              | `runtime/docker_bridge_artifacts.py:402-414`              | Writes `summary.json` with sorted keys + indent                                    | Pattern reference for the bundle manifest writer |
| `_get_version()`                               | `cli/main.py:15-20`, `runtime/evidence/_helpers.py:51-56` | `importlib.metadata.version("medre")` with `"0.1.0"` fallback                      | Yes — already embedded as `medre_version`        |
| `load_config()`                                | `config/loader.py:310-354`                                | Returns `(RuntimeConfig, ConfigSource, MedrePaths)`                                | Yes — used by every config-backed path           |
| Exit codes                                     | `cli/exit_codes.py`                                       | `EXIT_OK=0`, `EXIT_CONFIG=2`, `EXIT_BUILD=3`, `EXIT_STARTUP=4`, `EXIT_NOT_FOUND=5` | Yes — see F-013                                  |

## Existing Redaction Inventory

| Redaction surface                              | Location                                     | What it covers                                                                                                                                                                                                   | Gaps                                                                                                                                                                                                                                                                   |
| ---------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `_SECRET_KEY_NAMES` frozenset                  | `config/_yaml.py:186-197`                    | 8 exact key names (`access_token`, `password`, `secret`, `api_key`, `apikey`, `token`, `private_key`, `client_secret`)                                                                                           | YAML-parser error messages only; case-sensitive equality on `.lower()`; misses `auth`, `credential`, `ble_address`, `identity_path`, `encryption_key`, `device_key`                                                                                                    |
| `_SECRET_FIELD_RE` regex                       | `config/env.py:201-204`                      | 8 case-insensitive substrings (`TOKEN`, `SECRET`, `PASSWORD`, `KEY`, `AUTH`, `CREDENTIAL`, `BLE`, `IDENTITY`)                                                                                                    | Env-var provenance only; substring match is greedy (`KEY` matches `keyboard`) — acceptable for redaction (over-redact is safe)                                                                                                                                         |
| `_SECRET_KEY_PATTERNS` regex tuple             | `core/observability/sanitization.py:26-42`   | 12 anchored regexes covering password, secret*, private_key*, access_token, auth_token, api_key, credential(s), session_secret, encryption_key, device_key, signing_key, identity_key                            | `sanitize_for_log()` only (drops whole keys from dicts); misses `ble_address`, `homeserver` (not a secret), `identity_path` (covered via `identity_key` partially)                                                                                                     |
| `_TOKEN_RE` in-string regex                    | `core/observability/sanitization.py:100-112` | 10 alternations: `syt_<alnum>+`, `MDAx<b64{20,}>`, `<b64{40,}>`, `sk-<alnum{20,}>`, `api[_-]?key=<value>`, `access_token=<value>`, `token=<value>`, `password=<value>`, `secret=<value>`, `credentials?=<value>` | Truncates to 512 chars; 40+ char base64 branch may over-redact uniform-character strings (changelog notes the negative lookahead was removed to avoid catastrophic backtracking); does not catch secrets in `<key>: <value>` (YAML-style) format, only `<key>=<value>` |
| `_SDK_RE` regex                                | `core/observability/sanitization.py:114`     | `<module.object at 0x...>` repr strings                                                                                                                                                                          | Defensive only; catches accidental object reprs                                                                                                                                                                                                                        |
| `sanitize_error()`                             | `core/observability/sanitization.py:117-137` | Applies `_TOKEN_RE` + `_SDK_RE` and truncates to 512 chars with `...` marker                                                                                                                                     | Already routed through every `_section_*()` helper in the evidence collector (`_helpers.py:82-90`)                                                                                                                                                                     |
| `sanitize_for_log()`                           | `core/observability/sanitization.py:77-88`   | Drops `_SECRET_KEY_PATTERNS` keys + recursively sanitises nested dicts/lists                                                                                                                                     | Not used by `_collect_config_summary()` today — bundle only exposes adapter wrapper metadata, not config internals                                                                                                                                                     |
| `EnvProvenance.redacted_items()`               | `config/env.py:263-293`                      | Returns `[(name, value_or "***REDACTED***")]` using `_SECRET_FIELD_RE` on the field segment                                                                                                                      | Field-name heuristic only — a secret value bound to a benign-named env var is not redacted here (caught only if it later appears in a `sanitize_error()` path)                                                                                                         |
| `MedreEnvConfig.to_dict()`                     | `config/env.py:821-827`                      | Returns raw `{name: value}` — **unredacted by design**                                                                                                                                                           | Documented as unsafe; the bundle must never call this for operator-facing output                                                                                                                                                                                       |
| `redact_config_snapshot()`                     | `runtime/docker_bridge_artifacts.py:265-271` | Wraps `sanitize_for_log()` for a full config dict                                                                                                                                                                | Drops `_SECRET_KEY_PATTERNS` keys — same coverage gap as `sanitize_for_log()` (e.g. `ble_address`, `identity_path` not in the 12-pattern list)                                                                                                                         |
| `_redact_strings()`                            | `runtime/docker_bridge_artifacts.py:358-372` | Recursive walker applying `sanitize_error()` to every string value                                                                                                                                               | Defence-in-depth; operates on values not keys; docker-bridge summary only                                                                                                                                                                                              |
| `_redact_key()`                                | `config/_yaml.py:200-210`                    | Returns `<redacted key 'NAME'>` for secret-named keys in YAML duplicate-key errors                                                                                                                               | Error-message rendering only; not a general-purpose redactor                                                                                                                                                                                                           |
| `_format_mark()` / `_sanitize_yaml_error()`    | `config/_yaml.py:218-260`                    | Strip YAML buffer snippets from parser error messages                                                                                                                                                            | Defensive — never echoes raw file content                                                                                                                                                                                                                              |
| `_yaml_escape_string()` / `_format_yaml_key()` | `runtime/docker_bridge_artifacts.py:482+`    | YAML-safe escaping for redacted config output                                                                                                                                                                    | Output-layer safety; not a redactor                                                                                                                                                                                                                                    |

## Coverage Matrix

| Audit area                          | Status  | Notes                                                                                                              |
| ----------------------------------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| Diagnostics CLI surface mapped      | Done    | `main.py:71-82`; flat command, no subparsers (F-003)                                                               |
| Evidence bundle collector read      | Done    | `_bundle.py:43-252`; six sections + seven hoisted surfaces (F-001)                                                 |
| Evidence CLI command read           | Done    | `evidence_commands.py:13-73`; storage-path-only surface (F-002)                                                    |
| Config loader + ConfigSource        | Done    | `loader.py:49-56, 310-354`; JSON-safe `.value` (F-008)                                                             |
| Env override provenance             | Done    | `env.py:230-307, 821-827`; field-name heuristic redaction (F-006, F-018)                                           |
| YAML strict parser redaction        | Done    | `_yaml.py:186-197, 200-260`; error-message only                                                                    |
| Sanitisation primitives             | Done    | `sanitization.py:26-137`; five overlapping surfaces (F-005)                                                        |
| Storage config fields               | Done    | `model.py:225-230`; `backend`, `path` — both safe                                                                  |
| Logging config fields               | Done    | `model.py:177-200`; `level`, `format`, `overrides` — all safe                                                      |
| Retry config fields                 | Done    | `model.py:203-222`; `enabled`, `interval_seconds`, `batch_size`, `max_attempts` — all safe                         |
| Adapter config wrapper fields       | Done    | `model.py:306-471`; `adapter_id`, `enabled`, `adapter_kind`, `config` — wrapper safe, config introspection avoided |
| Route plan model                    | Done    | `route_plan.py:50-158`; frozen dataclasses, JSON-safe (F-007)                                                      |
| Runtime snapshot contract           | Done    | `snapshot.py:1-120`; 17 sections, no secrets (F-016)                                                               |
| Version metadata                    | Done    | `pyproject.toml:3`, `cli/main.py:15-20`; duplicated helper (F-010)                                                 |
| Existing ZIP/tarball writer         | Absent  | No `zipfile` / `tarfile` usage in `src/` (F-004)                                                                   |
| Docker-bridge artifact pattern      | Done    | `docker_bridge_artifacts.py:232-414`; closest precedent for directory-of-files + manifest                          |
| Diagnostics redaction tests         | Done    | `test_cli_diagnostics_commands.py:113-132`, `test_cli_diagnostics_workflows.py:72-75`                              |
| Sanitiser hardening tests           | Done    | `test_sanitizer_hardening.py` (434 lines), `test_bounded_evidence_safety.py` (549 lines)                           |
| Storage-only secret-leakage tests   | Done    | `test_storage_only_evidence.py:391-435`                                                                            |
| Snapshot stress / token regex tests | Done    | `test_snapshot_stress.py:975-978` (parametrised), `test_snapshot_schema_stability.py:1277`                         |
| Env-var redaction tests             | Done    | `test_config_env.py:731, 758, 781, 831, 841`; `test_config_env_first.py:388`                                       |
| Operator docs for bundle collection | Partial | `docs/ops/diagnostics-and-evidence.md:37-87`; no "file a support issue" guidance (F-012)                           |
| Operator docs for what to attach    | Missing | `docs/ops/troubleshooting.md` lacks attach-to-issue section (F-012)                                                |
| Existing dev audits consulted       | Done    | `operator-surface-audit.md`, `runtime-evidence-completeness-audit.md` — both cover overlapping ground              |
| Bundle JSON schema                  | Done    | `docs/schemas/evidence-bundle.schema.json` (798 lines); frozen at `schema_version: 1`                              |
| Recover runbook model               | Done    | `recover_commands.py`, `core/recovery/`; not currently a bundle member (F-011)                                     |
| Storage status helper               | Done    | `storage_commands.py:20-95`; human-readable only (F-017)                                                           |
| Exit-code contract for bundle       | Partial | `exit_codes.py` defines codes; bundle-specific mapping needed (F-013)                                              |
| Convergence-query truncation        | Done    | `_storage_sections.py:25-26, 445-462`; 10K row cap with warning (F-014)                                            |

## Intentionally Deferred

The following are intentionally out of scope for the
`medre diagnostics bundle` change. Each is tracked here so future
work can pick it up without re-auditing.

1. **Redaction-surface unification** (F-005). Consolidating the five
   overlapping secret-detection implementations into a single source
   of truth is a separate refactor with its own test surface. The
   new bundle command should reuse `sanitize_for_log()` and
   `sanitize_error()` as-is and not introduce a sixth pattern.
2. **`MedreEnvConfig.to_dict()` deprecation** (F-018). Renaming to
   `to_unredacted_dict()` to make the unsafe shape obvious at call
   sites is worthwhile but touches every caller; defer to a
   cleanup change.
3. **Per-event recovery runbook in the bundle** (F-011). The
   `incident_summary` field in the storage section already includes
   `recommended_commands`. Embedding the full
   `medre recover --event` runbook is additive and can wait for
   operator feedback.
4. **Storage health as a structured bundle member** (F-017). The
   storage section of the evidence bundle already reports
   `db_exists`, `db_path`, `event_count`, `receipt_count`, and any
   read error. The schema-version + per-table column check from
   `medre storage status` is a nice-to-have, not a blocker.
5. **Tarball / gzip output** (F-004). ZIP via stdlib `zipfile` is
   sufficient and universally openable. Adding `.tar.gz` support
   introduces nothing the operator cannot get from `unzip | tar czf -`.
6. **Bundle encryption at rest**. Operators who need to share a
   bundle over an untrusted channel should use age/gpg/openssl
   outside MEDRE. Adding an `--encrypt` flag would import a crypto
   dependency and expand the trust boundary; out of scope.
7. **Continuous / scheduled bundle collection**. The bundle is a
   point-in-time snapshot by design (`_LIMITATIONS[0]` in
   `_helpers.py:38`). Scheduled collection is an operator-workflow
   concern (cron, systemd timer), not a MEDRE feature.
8. **Bundle diff tooling**. Comparing two bundles to diagnose drift
   is valuable but belongs in a separate analysis tool, not the
   collector. The manifest-with-SHA-256 pattern recommended in
   F-004 makes external diff straightforward.
9. **Live-health polling loop in the bundle**. The bundle collects
   one live-health refresh via `--include-refresh-health`. A
   sustained polling loop is monitoring, not a support bundle;
   out of scope per `_LIMITATIONS[3]`.
