# Config Schema Authority Audit

Date: 2026-06-15
Branch: config-schema-authority-hardening
Auditor: deep agent (Wave 1)

## Summary

The MEDRE config layer is in a mostly-healthy state after the YAML and
route-context-label migrations. The typed model in
`src/medre/config/model.py` + `routes.py` is internally coherent, the YAML
loader (`_yaml.py`) enforces the documented boring subset, discovery rejects
`.toml` with the dedicated migration message, the spec routing-delivery.md
§17.5/§17.6 and `docs/ops/configuration.md` describe `channel_room_map`
structured entries, per-entry origin labels, and same-room fan-in correctly,
and the strict-YAML test suite (`tests/test_config_yaml_strict.py`) covers
duplicate keys, anchors, aliases, merge keys, custom tags, secret redaction,
and non-mapping top-level thoroughly.

There are **three blocking mismatches** that must be fixed before this tranche
ships. The most serious is **F-002**: four shipped example configs
(`lxmf-receiver.yaml`, `lxmf-sender.yaml`, `meshcore-lab.yaml`,
`meshcore-tbeam.yaml`) use `adapter_kind: lxmf` / `adapter_kind: meshcore`,
which the typed runtime wrappers reject with `ConfigValidationError`. Those
configs are not in the `REQUIRED_YAML_CONFIGS` list in
`tests/test_example_configs.py`, so CI does not catch the break. The second
is **F-001**: `docs/schemas/adapter-config.schema.json` is missing the
Meshtastic packet-routing fields (`encrypted_action`, `chat_portnums`,
`disabled_portnums`, `detection_sensor_relay`) that the typed
`MeshtasticConfig` carries. The third is **F-005/F-006**: the
`docs/spec/configuration.md` YAML schema block and adapter documentation are
stale relative to the typed model — `limits:` is shown as a top-level
section (it lives under `runtime.limits`), `device_id` is shown as
operator-facing (it is internal), and `channel_room_map` /
`source_origin_label` / `dest_origin_label` / same-room fan-in are absent.

The remainder are stale wording (test fixture `.toml` extensions that do not
load, schema examples missing new-field demonstrations, internal spec
inconsistencies between §17.5.2 and §17.5.8), plus a handful of test
coverage gaps around unknown-key rejection at root, route, and adapter
levels. None of those are load-bearing for runtime behavior.

## Methodology

Static read-only analysis of:

- The typed config source of truth: `src/medre/config/{model,loader,_yaml,
env,paths,sample,errors,routes,adapters/*}.py`.
- The CLI surface: `src/medre/cli/{main,config_commands}.py`.
- Both JSON Schemas: `docs/schemas/routing-config.schema.json`,
  `docs/schemas/adapter-config.schema.json`.
- All shipped example configs under `examples/configs/*.yaml` and the
  README.
- Spec and operator docs: `docs/spec/configuration.md`,
  `docs/spec/routing-delivery.md`, `docs/spec/transport-profiles/*.md`,
  `docs/ops/configuration.md`, `docs/ops/running-medre.md`,
  `docs/ops/troubleshooting.md`, `docs/changes/unreleased.md`.
- The five test files named in the audit scope plus
  `tests/test_config_yaml_strict.py`, `tests/test_config_model.py`, and
  grep-based inventory of test coverage gaps.
- Repo-wide `grep` for `medre.toml`, `config.toml`, `.toml`,
  `meshnet_name`, `matrix_relay_prefix`, and the old placeholder set
  (`{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`).

No tests were executed; this is static analysis only.

## Findings

### [F-001] adapter-config.schema.json missing Meshtastic packet-routing fields

- **Category**: blocking schema/doc mismatch
- **Location**: `docs/schemas/adapter-config.schema.json:87-187` (the
  `MeshtasticConfig` arm of the `oneOf`); compare
  `src/medre/config/adapters/meshtastic.py:135-138`.
- **Current state**: The schema lists 16 properties on `MeshtasticConfig`
  (`adapter_id`, `connection_type`, `host`, `port`, `serial_port`,
  `ble_address`, `origin_label`, `default_channel`, `channel_mapping`,
  `message_delay_seconds`, `startup_backlog_suppress_seconds`,
  `sync_timeout_ms`, `radio_relay_prefix`, `mmrelay_compatibility`,
  `max_text_bytes`, `queue_send_max_attempts`, `outbound_mode`).
- **Expected state**: The typed dataclass at
  `src/medre/config/adapters/meshtastic.py:135-138` also declares
  `encrypted_action: Literal["drop", "deferred"] = "drop"`,
  `chat_portnums: frozenset[str]`,
  `disabled_portnums: frozenset[str]`, and
  `detection_sensor_relay: bool = False`. These were added by the
  "Meshtastic Configurable Packet Routing" change fragment
  (`docs/changes/unreleased.md:55`). The schema has
  `"additionalProperties": false`, so any instance document (or YAML
  config) that sets these fields would FAIL schema validation despite
  being accepted by the typed loader.
- **Recommendation**: Add the four fields to the `MeshtasticConfig` arm
  of `adapter-config.schema.json` with the correct types and defaults
  (`encrypted_action` enum `["drop", "deferred"]` default `"drop"`;
  `chat_portnums` / `disabled_portnums` as `array` of `string`,
  `uniqueItems: true`, `default: []`; `detection_sensor_relay` `boolean`
  default `false`). Add a corresponding assertion in
  `tests/test_docs_schema_examples.py::TestSourceDriftDetection` so
  future field additions on `MeshtasticConfig` fail the test.

### [F-002] Four example configs use invalid `adapter_kind` values

- **Category**: blocking schema/doc mismatch
- **Location**:
  `examples/configs/lxmf-receiver.yaml:13` (`adapter_kind: lxmf`),
  `examples/configs/lxmf-sender.yaml` (same),
  `examples/configs/meshcore-lab.yaml:9` (`adapter_kind: meshcore`),
  `examples/configs/meshcore-tbeam.yaml` (same).
- **Current state**: Each of these four minimal configs declares
  `adapter_kind: <transport-name>`. The typed runtime wrappers
  (`LxmfRuntimeConfig.from_dict` at
  `src/medre/config/model.py:394-417`, `MeshCoreRuntimeConfig.from_dict`
  at `src/medre/config/model.py:359-382`, and the parallel Matrix /
  Meshtastic wrappers) all enforce
  `if adapter_kind not in ("real", "fake"): raise ConfigValidationError`
  via the `transport` / `adapter_id` / `section_path` kwargs. Loading any
  of these four configs via `medre run --config <path>` would exit with
  `ConfigValidationError` before any adapter is built.
- **Expected state**: `adapter_kind` accepts only `"real"` or `"fake"`.
  These examples should use `adapter_kind: real` (they describe real
  hardware bring-up) or omit the field entirely (default is `"real"`).
- **Recommendation**: Replace `adapter_kind: lxmf` → `adapter_kind: real`
  in both lxmf configs and `adapter_kind: meshcore` → `adapter_kind: real`
  in both meshcore configs. Add the four files to
  `tests/test_example_configs.py::REQUIRED_YAML_CONFIGS` (see F-016) so
  the test_adapter_kinds_valid parametrization catches regressions.

### [F-003] lxmf/meshcore minimal examples document `TRANSPORT` as if it were a YAML field

- **Category**: stale wording
- **Location**:
  `examples/configs/lxmf-receiver.yaml:11-18`,
  `examples/configs/lxmf-sender.yaml:11-18`,
  `examples/configs/meshcore-lab.yaml:9-16`,
  `examples/configs/meshcore-tbeam.yaml:9-16`.
- **Current state**: The comment block in each file shows both forms:
  "Wire identity and display name via env" (correct —
  `MEDRE_ADAPTER__<TOKEN>__IDENTITY_PATH` is an env override), followed
  by "Or create entirely from env" which lists
  `MEDRE_ADAPTER__LXMF_RECEIVER__TRANSPORT=lxmf`. The latter is valid
  **only for env-created adapters** per
  `src/medre/config/env.py:387-401` (`_TRANSPORT_REGISTRY`) and
  `env.py:1045-1057`. In a YAML config the transport is taken from the
  section path (`adapters.lxmf.<name>`), so a YAML comment suggesting
  `TRANSPORT` is a YAML key is misleading.
- **Expected state**: Comments should clarify that `TRANSPORT` is the
  env-only creation trigger, not a YAML field. Better: drop the
  "create entirely from env" sub-block from these minimal configs and
  cross-reference `docs/ops/configuration.md` §"Env-First Adapter
  Creation" instead.
- **Recommendation**: Trim the misleading comment blocks; add a
  one-liner pointing at the operator doc for env-first adapter creation.

### [F-004] test_example_configs.py excludes four shipped configs from validation

- **Category**: missing test coverage
- **Location**: `tests/test_example_configs.py:30-47`.
- **Current state**: `REQUIRED_YAML_CONFIGS` lists 10 files and
  `PLACEHOLDER_CREDENTIAL_CONFIGS` lists one.
  `ALL_SHIPPED_CONFIGS = REQUIRED_YAML_CONFIGS +
PLACEHOLDER_CREDENTIAL_CONFIGS` drives `test_adapter_kinds_valid`,
  `test_no_real_secrets`, `test_no_deprecated_language`,
  `test_uses_supported_storage_backend`. The four minimal configs from
  F-002 (`lxmf-receiver.yaml`, `lxmf-sender.yaml`, `meshcore-lab.yaml`,
  `meshcore-tbeam.yaml`) are NOT in either list. They are only reached
  by `_ALL_CONFIG_FILES = sorted(CONFIGS_DIR.glob("*.yaml"))` in
  `TestEnvVarDocumentation`, which only inspects `${VAR}` patterns.
- **Expected state**: Every shipped `examples/configs/*.yaml` file
  should be in `ALL_SHIPPED_CONFIGS` so that adapter_kind, secret, and
  deprecated-language scanners cover them. Alternatively, define a new
  `MINIMAL_CONFIGS` group that at least runs
  `test_adapter_kinds_valid` against them.
- **Recommendation**: Add the four files to `REQUIRED_YAML_CONFIGS`
  (after fixing F-002), or to a new list that runs the adapter_kind
  check. This is the test-side fix that prevents F-002 from recurring.

### [F-005] docs/spec/configuration.md §3 YAML schema block is stale

- **Category**: blocking schema/doc mismatch
- **Location**: `docs/spec/configuration.md:85-169` (the YAML Schema
  block).
- **Current state**:
  - Lines 102-106 show `limits:` as a TOP-LEVEL section. The typed
    loader reads it from `runtime_data.get("limits", {})` at
    `src/medre/config/loader.py:293`, so the correct YAML path is
    `runtime.limits`. A config that puts `limits:` at the top level
    silently gets the defaults.
  - Line 126 in the Matrix adapter sample shows `device_id: MEDREBOT`
    as if it were operator-facing. Per
    `src/medre/config/adapters/matrix.py:38-41` and
    `src/medre/config/sample.py:75-78`, `device_id` is internal/test
    only.
  - Lines 116-128 (Matrix adapter) omit `adapter_kind`, `origin_label`,
    `relay_prefix`, `require_encrypted_rooms`, `auto_join_rooms`.
  - Lines 129-135 (Meshtastic) omit `adapter_kind`, `connection_type:
fake`, `origin_label`, and the four packet-routing fields
    (`encrypted_action`, `chat_portnums`, `disabled_portnums`,
    `detection_sensor_relay`).
  - Lines 137-143 (MeshCore) omit `ble`, `origin_label`, `serial_baudrate`,
    `ble_pin`, `meshcore_relay_prefix`, and `max_text_bytes`.
  - Lines 145-150 (LXMF) omit `storage_path`, which is REQUIRED for
    `connection_type: "reticulum"` per
    `src/medre/config/adapters/lxmf.py:221-225`. The sample shows
    `connection_type: reticulum` without `storage_path`, so it would
    fail validation.
- **Expected state**: The spec YAML schema block should match the typed
  model exactly (or, equivalently, the operator doc
  `docs/ops/configuration.md` already does this correctly — cross-
  reference it instead of maintaining a divergent copy).
- **Recommendation**: Rewrite §3 to (a) put `limits` under `runtime:`,
  (b) drop the `device_id` line from the operator-facing sample (move
  it to a comment marked "internal/test only" as `sample.py` does), (c)
  add `adapter_kind` to each adapter block, (d) add the missing
  Meshtastic/MeshCore/LXMF fields, (e) make the LXMF sample include
  `storage_path` or use `connection_type: fake`. The simplest fix is to
  replace the duplicated schema block with a pointer to
  `docs/ops/configuration.md` and a short note that the typed model is
  authoritative.

### [F-006] docs/spec/configuration.md silent on channel_room_map and per-entry origin labels

- **Category**: blocking schema/doc mismatch
- **Location**: `docs/spec/configuration.md` (whole document — search
  for `channel_room_map`, `source_origin_label`, `dest_origin_label`,
  `fan-in`, `duplicate room` returns zero hits).
- **Current state**: The spec configuration page does not mention
  structured `channel_room_map` entries, per-entry origin labels, the
  origin_label precedence chain (per-entry > route > adapter), the
  explicit-empty-string-suppresses-fallback rule, or the same-room
  fan-in / duplicate-Matrix-room semantics. Those topics live in
  `docs/spec/routing-delivery.md §17.5.8` and `§17.6`, and in
  `docs/ops/configuration.md §channel_room_map Shorthand` and
  `§Per-entry origin labels`.
- **Expected state**: The configuration spec page should at least
  cross-reference the routing-delivery sections so operators reading the
  spec top-down discover the structured-entry shape and the
  duplicate-room rules.
- **Recommendation**: Add a short "Routes" subsection to
  `docs/spec/configuration.md` that names `channel_room_map`,
  `source_origin_label`, `dest_origin_label`, and points at
  `routing-delivery.md §17.5.8` and `§17.6` for normative semantics.

### [F-007] docs/spec/configuration.md §3 missing `adapter_kind` in adapter YAML schema

- **Category**: blocking schema/doc mismatch (sub-finding of F-005,
  called out because it has independent fix surface).
- **Location**: `docs/spec/configuration.md:116-150`.
- **Current state**: None of the four adapter blocks in the spec sample
  mention `adapter_kind`. The typed model treats `adapter_kind` as one
  of three wrapper-level fields (`enabled`, `adapter_id`,
  `adapter_kind`) consumed by the runtime wrapper before the adapter
  dataclass is constructed (`src/medre/config/model.py:39-41`,
  `_WRAPPER_FIELD_NAMES`).
- **Expected state**: Adapter blocks should document
  `adapter_kind: real | fake` with default `"real"`.
- **Recommendation**: Add `adapter_kind` to each adapter sample and to
  the `RuntimeOptions`-style field tables. (Folded into F-005 fix.)

### [F-008] routing-delivery.md §17.5.2 origin_label levels contradict §17.5.8

- **Category**: blocking schema/doc mismatch (internal spec
  inconsistency).
- **Location**: `docs/spec/routing-delivery.md:1219-1239` (§17.5.2) vs
  `docs/spec/routing-delivery.md:1366-1409` (§17.5.8).
- **Current state**: §17.5.2 says `origin_label` "can be set at two
  levels" (route + adapter) and gives a 3-step precedence chain
  (route-level → adapter config → empty string). §17.5.8 introduces a
  THIRD level (per-entry labels on `channel_room_map` entries) with a
  4-step precedence chain (per-entry → route-level → adapter → empty).
  §17.5.2 also omits the "explicit empty string suppresses fallback"
  rule that §17.5.8 states.
- **Expected state**: §17.5.2 should be the canonical summary; it
  should either mention all three levels or explicitly defer to
  §17.5.8 for the per-entry level. Right now the two sections read as
  disagreeing about the same concept.
- **Recommendation**: Edit §17.5.2 to say "It can be set at three
  levels: per-entry (channel_room_map only), route, and adapter", add
  the per-entry step to the precedence chain, and add a sentence
  stating "An explicit empty string at any level suppresses fallback
  below that level; see §17.5.8."

### [F-009] routing-delivery.md does not list removed template placeholders as unknown

- **Category**: stale wording
- **Location**: `docs/spec/routing-delivery.md:1251-1304` (§17.5.3
  through §17.5.5).
- **Current state**: §17.5.3 lists the canonical template variables
  (`{origin_label}`, `{sender_id}`, `{sender}`, `{route_id}`). §17.5.5
  notes "Unknown placeholders are left unchanged in the output and
  recorded in diagnostic metadata", but does not enumerate the legacy
  placeholders that the formatter no longer resolves. The change
  fragment at `docs/changes/unreleased.md:142-150` ("Clean Attribution
  Surface — Canonical Variables Only") explicitly says
  `{longname}`, `{shortname}`, `{shortname5}`, `{from_id}`,
  `{meshnet_name}` are unknown placeholders, but that migration note
  is not reflected in the spec body.
- **Expected state**: A short "Removed placeholders" table in §17.5.5
  listing the five legacy placeholders and stating they are passed
  through as literals.
- **Recommendation**: Add a table after the §17.5.5 formatter-rules
  paragraph:

  | Removed placeholder | Current behavior          |
  | ------------------- | ------------------------- |
  | `{meshnet_name}`    | Unknown — left as literal |
  | `{longname}`        | Unknown — left as literal |
  | `{shortname}`       | Unknown — left as literal |
  | `{shortname5}`      | Unknown — left as literal |
  | `{from_id}`         | Unknown — left as literal |

### [F-010] routing-config-example.json missing new route fields

- **Category**: missing example coverage
- **Location**: `docs/schemas/examples/routing-config-example.json`.
- **Current state**: The example demonstrates a single route with
  `channel_room_map: {"0": "!roomkey:matrix.example.org"}` (bare-string
  shape only). It does NOT set `source_origin_label` or
  `dest_origin_label` at the route level, does not demonstrate the
  structured `channel_room_map` entry shape
  (`{room, source_origin_label, dest_origin_label}`), and is therefore
  not exercising the new schema branches at
  `docs/schemas/routing-config.schema.json:74-101` and `$defs/
ChannelRoomMapEntry`.
- **Expected state**: The example should demonstrate at least one
  structured `channel_room_map` entry and one route-level
  `source_origin_label` so schema validation tests in
  `tests/test_docs_schema_examples.py` exercise the new shape.
- **Recommendation**: Extend the example to include a second channel
  key using the structured shape, e.g.

  ```json
  "channel_room_map": {
    "0": "!roomkey:matrix.example.org",
    "1": {
      "room": "!ops:matrix.example.org",
      "source_origin_label": "Ops",
      "dest_origin_label": "Matrix-Ops"
    }
  },
  "source_origin_label": "Default"
  ```

### [F-011] adapter-config-example.json is Matrix-only and incomplete

- **Category**: missing example coverage
- **Location**: `docs/schemas/examples/adapter-config-example.json`.
- **Current state**: The single example is a Matrix config with 11
  fields populated. It is missing `origin_label` and `relay_prefix`
  (both of which are in the schema at
  `docs/schemas/adapter-config.schema.json:73-82`). There are no
  example payloads for the `MeshtasticConfig`, `MeshCoreConfig`, or
  `LxmfConfig` arms of the schema's `oneOf`.
- **Expected state**: Either add four separate example files
  (`adapter-config-meshtastic-example.json`, etc.) or extend
  `tests/test_docs_schema_examples.py` to construct example payloads
  in-code for each arm of the `oneOf`. The Matrix example should also
  demonstrate `origin_label` and `relay_prefix`.
- **Recommendation**: At minimum, add `origin_label` and `relay_prefix`
  to the existing Matrix example. Adding per-transport example files
  is the preferred larger fix and pairs naturally with the F-001
  schema fix.

### [F-012] Unknown root config keys silently dropped — no rejection behavior or test

- **Category**: missing test coverage (and arguably a typed-model gap).
- **Location**: `src/medre/config/loader.py:278-375`
  (`_parse_runtime_config`); compare
  `src/medre/config/routes.py:160-169` where `BridgePolicy.from_dict`
  DOES reject unknown keys.
- **Current state**: `_parse_runtime_config` reads sections via
  `data.get("runtime", {})`, `data.get("logging", {})`,
  `data.get("storage", {})`, `data.get("retry", {})`,
  `data.get("adapters", {})`, `data.get("routes", {})`. There is no
  final `set(data.keys()) - KNOWN_ROOT_KEYS` check. A config with a
  typo at the root (e.g. `roues:` instead of `routes:`, or `loging:`
  instead of `logging:`) is silently accepted and the operator gets
  the default for the section they thought they were configuring.
- **Expected state**: Per the task brief ("the typed config model is
  authoritative"), unknown root keys should be rejected with a
  `ConfigValidationError` carrying `section_path="<root>"`. At
  minimum, there should be a test documenting the current behavior.
- **Recommendation**: Decide intent. If rejection is desired, add a
  `_KNOWN_ROOT_KEYS = frozenset({"runtime", "logging", "storage",
"retry", "adapters", "routes"})` constant and an explicit unknown-key
  check at the end of `_parse_runtime_config`. If silently dropping is
  intentional (to keep the loader lenient for forward-compat), add a
  test in `tests/test_config_loader.py` that documents the behavior
  and add a comment in the loader noting the design choice.

### [F-013] Unknown adapter keys silently dropped — codified as desired behavior

- **Category**: missing test coverage (but intentional).
- **Location**: `src/medre/config/model.py:54-77` (`_coerce_adapter_kwargs`,
  specifically `if key not in valid_names: continue`); codified by
  `tests/test_config_model.py:114-118` (`test_unknown_key_ignored`).
- **Current state**: Any key in an `adapters.<transport>.<instance>`
  table that does not match a dataclass field name is silently
  dropped. A typo like `conection_type: serial` produces no error and
  the adapter gets `connection_type="fake"` (the default).
- **Expected state**: Per the task brief, this should be a deliberate
  decision. The current code + test makes it deliberate. The JSON
  schema (`additionalProperties: false` on each adapter arm) DISAGREES
  with the typed model — the schema rejects unknown keys, the loader
  accepts them.
- **Recommendation**: Pick one. Either (a) tighten the loader to
  reject unknown adapter keys (preferred — matches schema and gives
  operators feedback on typos), or (b) relax the schema to
  `additionalProperties: true` and document the lenient stance in the
  spec. The current state where schema and loader disagree is the
  worst of both.

### [F-014] Unknown route-level keys silently dropped

- **Category**: missing test coverage.
- **Location**: `src/medre/config/routes.py:769-1089`
  (`RouteConfig.from_dict`).
- **Current state**: The method does `data = dict(data)` then a
  sequence of `data.pop(...)` calls for each known field
  (`source_adapters`, `dest_adapters`, `directionality`, `enabled`,
  `filter_hooks`, `source_channel`, `dest_channel`, `source_room`,
  `dest_room`, `source_origin_label`, `dest_origin_label`,
  `channel_room_map`, `policy`, `retry`). There is NO final check that
  `data` is empty. Unknown route-level keys are silently dropped.
- **Expected state**: `BridgePolicy.from_dict` at
  `src/medre/config/routes.py:160-169` rejects unknown policy keys
  with a clean error; `RouteConfig.from_dict` should do the same for
  unknown route-level keys.
- **Recommendation**: After the existing `pop()` sequence but before
  the final `cls(...)` construction, add

  ```python
  if data:
      unknown = sorted(data.keys(), key=lambda k: (type(k).__name__, repr(k)))
      raise ConfigValidationError(
          f"Route {route_id!r}: unknown key(s) {unknown}",
          section_path=section_path,
      )
  ```

  Add coverage in `tests/test_routes.py` or
  `tests/test_routes_channel_room_map.py`.

### [F-015] Four shipped configs missing from test_example_configs.REQUIRED_YAML_CONFIGS

- **Category**: missing test coverage (same scope as F-004; called out
  separately because F-004 is about a runtime bug while F-015 is about
  the test-list gap that hides it).
- **Location**: `tests/test_example_configs.py:30-47`.
- **Current state**: The list contains 10 entries. The four minimal
  configs (`lxmf-receiver.yaml`, `lxmf-sender.yaml`, `meshcore-lab.yaml`,
  `meshcore-tbeam.yaml`) are absent.
- **Expected state**: Every `*.yaml` file in `examples/configs/` should
  be in `REQUIRED_YAML_CONFIGS` (or in a new
  `MINIMAL_PLACEHOLDER_CONFIGS` list if their credentials are
  intentionally empty), so that the parametrized scanners
  (`test_no_real_secrets`, `test_adapter_kinds_valid`,
  `test_uses_supported_storage_backend`, `test_no_deprecated_language`)
  cover them.
- **Recommendation**: Either extend `REQUIRED_YAML_CONFIGS` to all 14
  shipped YAMLs, or add a new `MINIMAL_CONFIGS` list whose tests run
  `test_adapter_kinds_valid` and `test_yaml_valid` on them. Pair with
  the F-002 fix.

### [F-016] test_config_runtime_parity.py TestExampleConfigsUseSameLoader covers only 4 configs

- **Category**: missing test coverage.
- **Location**: `tests/test_config_runtime_parity.py:372-380`
  (`TestExampleConfigsUseSameLoader.ALL_CONFIGS`).
- **Current state**: `DIRECT_CONFIGS = ["fake-bridge-smoke.yaml",
"fake-multi-adapter.yaml"]` and `RESOLVED_CONFIGS =
["docker-matrix-bridge.yaml", "docker-meshtastic-bridge.yaml"]`.
  These four configs are the only ones driven through the full
  `load_config → RuntimeBuilder.build()` path in this test file.
- **Expected state**: The configs that claim to be runnable
  (`live-matrix-meshtastic.yaml`,
  `live-matrix-meshtastic-channel-map.yaml`, `matrix.yaml`,
  `meshtastic-serial.yaml`, `mixed-matrix-meshtastic.yaml`) should
  also go through `load_config` at minimum, even if credentials
  prevent a full `RuntimeBuilder.build()`. Adding the four minimal
  lxmf/meshcore configs from F-002 would have caught the
  `adapter_kind: lxmf` bug.
- **Recommendation**: Extend `DIRECT_CONFIGS` to include the four
  minimal configs once F-002 is fixed; add a separate
  `CREDENTIAL_REQUIRED_CONFIGS` list for `matrix.yaml`,
  `meshtastic-serial.yaml`, `mixed-matrix-meshtastic.yaml`,
  `live-matrix-meshtastic.yaml`,
  `live-matrix-meshtastic-channel-map.yaml` that runs
  `test_loads_via_load_config` and asserts the expected
  credential/hardware error class.

### [F-017] Test fixtures construct `MedrePaths` with `.toml` config_file paths

- **Category**: stale wording
- **Location**:
  `tests/test_replay_cancellation_shutdown.py:71`,
  `tests/test_replay_partial_failure.py:75`,
  `tests/test_persistence_authority_replay.py:79`, `:144`, `:244`,
  `tests/test_replay_bridge_conditions.py:72`.
- **Current state**: Each of these tests constructs a `MedrePaths`
  directly with `config_file=tmp / "config" / "config.toml"`. The path
  is only used to populate the `MedrePaths.config_file` attribute for
  runtime state; no config is loaded from it. The `.toml` extension is
  misleading because MEDRE no longer supports TOML runtime configs.
- **Expected state**: Test fixtures should use `config.yaml` (or any
  non-TOML extension) to avoid implying TOML is still loadable.
- **Recommendation**: Replace `config.toml` with `config.yaml` in the
  five fixture paths. No behavioral change.

### [F-018] Failure-path test strings use `/nonexistent/config.toml`

- **Category**: stale wording
- **Location**: `tests/test_smoke_runtime_evidence.py:152`,
  `tests/test_runtime_evidence_completeness.py:88`.
- **Current state**: Both tests pass `/nonexistent/config.toml` as the
  config path to exercise the failure path. Because the file does not
  exist, the loader raises `ConfigNotFoundError` before the suffix
  check runs (`src/medre/config/loader.py:171-173`). The `.toml`
  extension is irrelevant to the test and misleading to readers.
- **Expected state**: Use `/nonexistent/config.yaml` to avoid implying
  TOML is still loadable.
- **Recommendation**: One-line replacement in both tests.

### [F-019] test_config_env.py uses `/etc/medre/medre.toml` as a MEDRE_CONFIG value

- **Category**: stale wording
- **Location**: `tests/test_config_env.py:916-921`.
- **Current state**: The test sets
  `monkeypatch.setenv("MEDRE_CONFIG", "/etc/medre/medre.toml")` and
  asserts `env.config_path == "/etc/medre/medre.toml"`. The test
  verifies env-var parsing only — no file is loaded, so the `.toml`
  suffix is harmless — but the test data suggests TOML is still a
  valid runtime config format.
- **Expected state**: Use a `.yaml` path in the test data.
- **Recommendation**: Replace the literal with
  `/etc/medre/medre.yaml`.

### [F-020] docker_bridge_artifacts.py globs `*.toml` for artifact collection

- **Category**: stale wording (borderline intentionally-deferred).
- **Location**: `src/medre/runtime/docker_bridge_artifacts.py:1263`.
- **Current state**: `_collect_inspect_artifacts` globs for `*.json`,
  `*.log`, `*.yaml`, `*.yml`, `*.toml` when collecting inspect-related
  artifacts from a Docker run directory. The `*.toml` pattern is
  defensive — if a Docker test happens to drop a `.toml` file (e.g. a
  leftover meshtasticd config), it gets collected as an artifact. It
  does not load or parse the file as a runtime config.
- **Expected state**: Either keep the glob (defensive collection is
  harmless) or drop `*.toml` to match the YAML-only runtime posture.
- **Recommendation**: Low priority. If touched, drop `*.toml` and add
  a `# ponytail: *.toml kept defensively; drop if Docker tests no
longer produce .toml artifacts` comment, or just drop it outright.

### [F-021] `additionalProperties: false` on adapter schema conflicts with loader's silent-drop behavior

- **Category**: blocking schema/doc mismatch (sub-finding of F-013,
  called out separately because it has independent fix surface).
- **Location**: `docs/schemas/adapter-config.schema.json:85, 186, 292,
371` (each arm of the `oneOf` sets `"additionalProperties": false`).
- **Current state**: Schema rejects unknown keys; loader accepts them
  (`_coerce_adapter_kwargs` at `src/medre/config/model.py:60-61`).
  Schema-first consumers (e.g. editor integrations, downstream
  validators) get a different answer than the runtime.
- **Expected state**: Schema and loader must agree. Either both
  reject, or both accept.
- **Recommendation**: Decide via F-013. If the loader is tightened,
  no schema change is needed. If the loader stays lenient, change the
  four `additionalProperties: false` to `true` and document.

### [F-022] No changelog fragment for this audit tranche

- **Category**: missing test coverage (process).
- **Location**: `docs/changes/unreleased.md`.
- **Current state**: The unreleased changelog has entries for the
  prior tranches ("Per-Context Origin Labels for channel_room_map
  Entries", "Duplicate-Room Fan-In for channel_room_map and
  Config-Constructor Rename", "Example Configs and Documentation Moved
  from TOML to YAML", etc.) but no entry for the
  config-schema-authority-hardening tranche itself. When the
  implementation waves land (fixing F-001, F-002, F-005, etc.), a
  fragment describing the schema/example/spec reconciliation should be
  appended.
- **Expected state**: A new section at the bottom of
  `docs/changes/unreleased.md` describing the schema/example/spec
  reconciliation, listing the affected files, and noting any breaking
  changes (none expected — F-002 fixes examples that never loaded).
- **Recommendation**: Add the fragment as part of the implementation
  wave that lands the largest fix. Note that `AGENTS.md` mentions a
  per-fragment directory `docs/changes/unreleased/NNN-*.md`, but the
  project actually uses a single `unreleased.md` file (see
  `docs/changes/README.md`); follow the file convention, not the
  AGENTS.md prose.

## Coverage Matrix

| Audit area                                                                            | Status                                              | Notes                                                                                                                                                                                                                                                                                                                                                                                                |
| ------------------------------------------------------------------------------------- | --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------ | -------- | --- | ---- | ---------- | --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| YAML loader behavior (safe loader, dup keys, tags, anchors, merge, top-level mapping) | ✅                                                  | `_yaml.py` enforces all six rules; `tests/test_config_yaml_strict.py` has 40+ tests covering each plus secret redaction.                                                                                                                                                                                                                                                                             |
| Discovery behavior (.yaml/.yml ordering, .toml rejection, legacy discovery)           | ✅                                                  | `_discover_yaml` prefers `.yaml` over `.yml` (`loader.py:100-106`). `_raise_if_legacy_toml` covers both `config.toml` and `medre.toml` (`loader.py:109-121`). `_validate_config_suffix` raises the dedicated migration message (`loader.py:75-91`). Tests in `tests/test_config_yaml_discovery.py` and `tests/test_config_yaml_loader.py:374-416`.                                                   |
| RuntimeConfig sections (runtime, logging, storage, limits, retry, adapters, routes)   | ⚠️                                                  | Typed model is correct (`model.py:514-528`). Spec doc misplaces `limits` (F-005).                                                                                                                                                                                                                                                                                                                    |
| Adapter config — Matrix                                                               | ✅ typed / ⚠️ schema / ⚠️ spec                      | Typed `MatrixConfig` complete with `origin_label`, `relay_prefix` (`adapters/matrix.py:81-82`). Schema has both fields. Spec omits both from the sample (F-005).                                                                                                                                                                                                                                     |
| Adapter config — Meshtastic                                                           | ✅ typed / ❌ schema / ⚠️ spec                      | Typed config has 4 packet-routing fields (`adapters/meshtastic.py:135-138`). Schema missing all 4 (F-001). Spec sample stale (F-005).                                                                                                                                                                                                                                                                |
| Adapter config — MeshCore                                                             | ✅ typed / ✅ schema / ⚠️ spec                      | Typed config complete. Schema includes `ble_pin`, `meshcore_relay_prefix`. Spec sample missing `ble`, `ble_pin`, `meshcore_relay_prefix` (F-005).                                                                                                                                                                                                                                                    |
| Adapter config — LXMF                                                                 | ✅ typed / ✅ schema / ⚠️ spec                      | Typed config requires `storage_path` for `reticulum` mode. Schema documents it. Spec sample shows `reticulum` without `storage_path` (F-005).                                                                                                                                                                                                                                                        |
| `origin_label` presence on all adapter configs                                        | ✅                                                  | All four adapter dataclasses declare `origin_label: str = ""` with bool-before-str validation.                                                                                                                                                                                                                                                                                                       |
| `meshnet_name` ABSENT from all adapter configs                                        | ✅                                                  | grep finds zero occurrences in `src/medre/config/adapters/`. All hits in repo are intentional (tests asserting absence, renderer comments, audit docs, changelog).                                                                                                                                                                                                                                   |
| `matrix_relay_prefix` ABSENT from MeshtasticConfig                                    | ✅                                                  | grep finds zero occurrences in `src/medre/config/adapters/meshtastic.py`. Hits elsewhere are intentional (function names like `_apply_matrix_relay_prefix` in renderer, migration docs).                                                                                                                                                                                                             |
| channel_room_map structured entries (room, source_origin_label, dest_origin_label)    | ✅ typed / ✅ schema / ✅ ops doc / ⚠️ spec doc     | Typed `ChannelRoomMapEntry` at `routes.py:369-423`. Schema `$defs/ChannelRoomMapEntry` matches. Ops doc `§Per-entry origin labels` documents shape and precedence. Spec `configuration.md` silent (F-006); normative semantics live in `routing-delivery.md §17.5.8`.                                                                                                                                |
| Route-level `source_origin_label` / `dest_origin_label`                               | ✅ typed / ✅ schema / ✅ ops doc / ⚠️ spec §17.5.2 | `RouteConfig` dataclass (`routes.py:766-767`). Schema documents both with correct precedence prose. Ops doc `§Per-entry origin labels` covers the precedence chain. Spec §17.5.2 only mentions 2 levels, not 3 (F-008).                                                                                                                                                                              |
| Origin label precedence (per-entry > route > adapter)                                 | ✅ typed / ✅ schema / ✅ ops doc / ⚠️ spec §17.5.2 | `_parse_channel_room_map_entry` + `ChannelRoomMapEntry` carry per-entry labels; precedence resolved at runtime. Schema description text is correct. §17.5.2 chain is missing per-entry level (F-008).                                                                                                                                                                                                |
| Explicit empty string suppresses fallback; null/unset falls back                      | ✅ typed / ✅ schema / ✅ docs                      | `ChannelRoomMapEntry.__eq__` + `routes.py:1400-1401` (spec §17.5.8). Schema descriptions on `source_origin_label` / `dest_origin_label` in `$defs/ChannelRoomMapEntry` are correct. Ops doc `§Per-entry origin labels` covers it.                                                                                                                                                                    |
| Same-room fan-in allowed when no Matrix→Meshtastic leg                                | ✅ typed / ✅ schema / ✅ docs                      | `routes.py:972-978` defers ambiguity check to runtime expansion. `src/medre/runtime/route_engine.py::_validate_duplicate_rooms_for_direction` enforces the rule (per changelog `docs/changes/unreleased.md:573-621`). Spec `§17.6` has the full directionality decision matrix. Ops doc `§channel_room_map Shorthand` summarizes. Schema does NOT model duplicate rooms as always invalid (correct). |
| Duplicate Matrix rooms rejected when Matrix→Meshtastic ambiguous                      | ✅                                                  | Same as above. Runtime check in `route_engine.py`.                                                                                                                                                                                                                                                                                                                                                   |
| Bidirectional duplicate-room maps rejected                                            | ✅                                                  | §17.6 decision matrix row 5 (`bidirectional → Rejected` for both orientations).                                                                                                                                                                                                                                                                                                                      |
| Duplicate channel keys rejected                                                       | ✅                                                  | `routes.py:980-988` raises `ConfigValidationError` on duplicate normalized channel.                                                                                                                                                                                                                                                                                                                  |
| Canonical Matrix room validation (`!` prefix)                                         | ✅                                                  | `_validate_room_string` at `routes.py:622-687`.                                                                                                                                                                                                                                                                                                                                                      |
| Alias room rejection (`#` prefix)                                                     | ✅                                                  | `routes.py:672-679`.                                                                                                                                                                                                                                                                                                                                                                                 |
| Env override semantics (`MEDRE_ADAPTER__<TOKEN>__<FIELD>`)                            | ✅                                                  | `src/medre/config/env.py:1-50` documents the pattern; `_TRANSPORT_REGISTRY` at `env.py:387-401` handles env-created adapters.                                                                                                                                                                                                                                                                        |
| Secret redaction (errors/logs)                                                        | ✅                                                  | `_yaml.py:_SECRET_KEY_NAMES` + `_redact_key` (`_yaml.py:178-202`). `env.py:_SECRET_FIELD_RE` regex covers `TOKEN                                                                                                                                                                                                                                                                                     | SECRET | PASSWORD | KEY | AUTH | CREDENTIAL | BLE | IDENTITY`. `MatrixConfig.**repr**` redacts token to 3-char preview (`adapters/matrix.py:224-237`). `tests/test_config_yaml_strict.py:305-360` tests parse errors don't leak nearby secrets. |
| Example configs — TOML references                                                     | ✅                                                  | No `*.toml` files in `examples/configs/`. README lists YAML files only.                                                                                                                                                                                                                                                                                                                              |
| Example configs — quoting (room IDs, MXIDs)                                           | ✅                                                  | `examples/configs/README.md:40-44` documents the quoting rule. Spot-checked configs all quote `"!room:server"` and `"@user:server"`.                                                                                                                                                                                                                                                                 |
| Example configs — no real tokens/keys/PINs                                            | ✅                                                  | `tests/test_example_configs.py::TestExampleHygiene::test_no_real_secrets` parametrizes over `ALL_SHIPPED_CONFIGS` and scans for `syt_*` and `BEGIN PRIVATE KEY` patterns.                                                                                                                                                                                                                            |
| Example demonstrating structured channel_room_map                                     | ✅                                                  | `examples/configs/live-matrix-meshtastic-channel-map.yaml:96-108` shows the structured shape (commented) plus bare-string shape (active).                                                                                                                                                                                                                                                            |
| Example demonstrating per-entry origin labels                                         | ✅                                                  | Same file, same block.                                                                                                                                                                                                                                                                                                                                                                               |
| Example demonstrating same-room fan-in                                                | ✅                                                  | Same file, lines 110-125 (commented "Fan-in alternative").                                                                                                                                                                                                                                                                                                                                           |
| `examples/configs/README.md` lists only YAML                                          | ✅                                                  | All 11 entries in the table are `.yaml`.                                                                                                                                                                                                                                                                                                                                                             |
| CLI `medre config sample` emits valid YAML                                            | ✅                                                  | `src/medre/config/sample.py:20-231` returns a YAML string.                                                                                                                                                                                                                                                                                                                                           |
| CLI `medre config sample` includes structured channel_room_map example                | ⚠️                                                  | The sample documents route-level `source_origin_label` / `dest_origin_label` in comments (`sample.py:138-149`) but does NOT include a `channel_room_map` example with the structured shape. Operators running `medre config sample` see no `channel_room_map` demonstration at all.                                                                                                                  |
| CLI `medre config check` accepts YAML                                                 | ✅                                                  | `cli/config_commands.py:55-64` calls `load_config`; loader accepts `.yaml`/`.yml`.                                                                                                                                                                                                                                                                                                                   |
| CLI `medre config check` rejects TOML with migration message                          | ✅                                                  | Delegates to `loader._validate_config_suffix` which raises `ConfigFileError` with `_TOML_NOT_SUPPORTED_MSG` (`loader.py:70-72, 87-88`).                                                                                                                                                                                                                                                              |
| CLI `medre config check` reports YAML parse errors clearly                            | ✅                                                  | `StrictYAMLError` inherits `ConfigFileError` and carries `path:line:column:` prefix (`_yaml.py:210-221`). CLI catches all `Exception` and prints `f"Config error: {exc}"` (`config_commands.py:62-64`).                                                                                                                                                                                              |
| CLI `medre config check` reports typed validation errors with section paths           | ✅                                                  | `ConfigValidationError` carries `section_path` (`errors.py:31-42`); route and adapter constructors populate it consistently.                                                                                                                                                                                                                                                                         |
| CLI errors leak secrets                                                               | ✅                                                  | YAML parser raises before values are formatted; `_redact_key` covers duplicate-key messages; `MatrixConfig.__repr__` redacts. No secret-leak path identified.                                                                                                                                                                                                                                        |
| CLI help mentions `medre.toml` or `config.toml`                                       | ✅                                                  | `tests/test_cli_command_help_hints.py` has zero TOML references. CLI `--config` help text in `main.py:46, 58, 75, 89, 93, 95, 103, 305` says "Path to config file" with no extension prescription.                                                                                                                                                                                                   |
| Smoke / run-session defaults point at deleted `.toml` examples                        | ✅                                                  | `cli/main.py:103-105` defaults `--config` to `examples/configs/fake-bridge-smoke.yaml` (YAML).                                                                                                                                                                                                                                                                                                       |
| routing-config schema matches typed route model                                       | ✅ (mostly)                                         | `routing-config.schema.json` properties match `RouteConfig` dataclass fields including `channel_room_map` polymorphism, `source_origin_label`, `dest_origin_label`. `$defs/BridgePolicy` and `$defs/RouteRetryConfig` field sets match.                                                                                                                                                              |
| adapter-config schema matches typed adapter model                                     | ❌                                                  | F-001: missing 4 Meshtastic packet-routing fields.                                                                                                                                                                                                                                                                                                                                                   |
| Schemas describe YAML-parsed shape (not TOML syntax)                                  | ✅                                                  | Both schemas describe JSON-compatible shapes produced by the YAML loader. No mention of TOML tables.                                                                                                                                                                                                                                                                                                 |
| Schemas reject unknown keys where typed model rejects them                            | ⚠️                                                  | Schema sets `additionalProperties: false` everywhere. Typed model rejects unknowns for `BridgePolicy` and `ChannelRoomMapEntry` but silently drops unknowns at root, route, and adapter levels (F-012, F-013, F-014, F-021).                                                                                                                                                                         |
| Schemas model structured channel_room_map entries                                     | ✅                                                  | `$defs/ChannelRoomMapEntry` with `room`, `source_origin_label`, `dest_origin_label`; `additionalProperties: false`.                                                                                                                                                                                                                                                                                  |
| Schemas reject invalid label types                                                    | ⚠️                                                  | Schema declares labels as `["string", "null"]`. Does NOT explicitly reject booleans because JSON Schema treats `bool` as a separate type from `string`. The typed loader's bool-before-string check is stricter than the schema.                                                                                                                                                                     |
| Schemas allow explicit empty string labels                                            | ✅                                                  | `minLength` not set on the label properties; empty string is valid.                                                                                                                                                                                                                                                                                                                                  |
| Schemas allow null/unset labels where typed config allows fallback                    | ✅                                                  | Labels are `["string", "null"]`.                                                                                                                                                                                                                                                                                                                                                                     |
| Schemas do NOT imply duplicate Matrix rooms are always invalid                        | ✅                                                  | Schema has no uniqueness constraint across entries. Correctly deferred to runtime.                                                                                                                                                                                                                                                                                                                   |
| Schemas mention TOML tables as user syntax                                            | ✅                                                  | No TOML mention in either schema.                                                                                                                                                                                                                                                                                                                                                                    |
| Schemas include `meshnet_name` or `matrix_relay_prefix`                               | ✅                                                  | grep returns zero hits in `docs/schemas/`.                                                                                                                                                                                                                                                                                                                                                           |
| docs/spec/configuration.md — YAML-only                                                | ⚠️                                                  | Top-of-page prose is YAML-only. The YAML schema block is stale (F-005).                                                                                                                                                                                                                                                                                                                              |
| docs/spec/configuration.md — boring subset documented                                 | ⚠️                                                  | §4 search-order paragraph mentions boring subset briefly (`configuration.md:181-183`). Not as detailed as `docs/ops/configuration.md:7-11`.                                                                                                                                                                                                                                                          |
| docs/spec/configuration.md — `.toml` rejection                                        | ✅                                                  | `configuration.md:181-183` mentions `.toml` rejection.                                                                                                                                                                                                                                                                                                                                               |
| docs/spec/configuration.md — channel_room_map structured entries                      | ❌                                                  | Not mentioned. See F-006.                                                                                                                                                                                                                                                                                                                                                                            |
| docs/spec/configuration.md — origin label precedence                                  | ❌                                                  | Not mentioned. See F-006.                                                                                                                                                                                                                                                                                                                                                                            |
| docs/spec/configuration.md — fan-in / duplicate-room semantics                        | ❌                                                  | Not mentioned. See F-006.                                                                                                                                                                                                                                                                                                                                                                            |
| docs/spec/configuration.md — `origin_label` not a routing key                         | ❌                                                  | Not mentioned in configuration.md (covered in routing-delivery.md §17.5.2).                                                                                                                                                                                                                                                                                                                          |
| docs/spec/configuration.md — `{origin_label}` formatter variable                      | ❌                                                  | Not mentioned.                                                                                                                                                                                                                                                                                                                                                                                       |
| docs/spec/configuration.md — old placeholders documented as unknown                   | ❌                                                  | Not mentioned.                                                                                                                                                                                                                                                                                                                                                                                       |
| docs/spec/routing-delivery.md — same-room fan-in                                      | ✅                                                  | §17.6 has the full directionality decision matrix.                                                                                                                                                                                                                                                                                                                                                   |
| docs/spec/routing-delivery.md — duplicate-room ambiguity                              | ✅                                                  | §17.6.1 explains the Meshtastic→Matrix safe / Matrix→Meshtastic ambiguous distinction.                                                                                                                                                                                                                                                                                                               |
| docs/spec/routing-delivery.md — `origin_label` semantics                              | ✅                                                  | §17.5.2 covers it, with the §17.5.2 vs §17.5.8 inconsistency in F-008.                                                                                                                                                                                                                                                                                                                               |
| docs/spec/transport-profiles/\*.md per-transport config accuracy                      | ✅                                                  | All four transport profiles cover `{origin_label}` precedence and the cross-platform guidance. `meshtastic.md:158` explicitly notes "no `matrix_relay_prefix` on MeshtasticConfig".                                                                                                                                                                                                                  |
| docs/ops/configuration.md — YAML-only operator instructions                           | ✅                                                  | `configuration.md:1-26` is YAML-only. No `medre.toml` / `config.toml` operator-facing references.                                                                                                                                                                                                                                                                                                    |
| docs/ops/configuration.md — boring subset guidance                                    | ✅                                                  | `configuration.md:7-11`.                                                                                                                                                                                                                                                                                                                                                                             |
| docs/ops/running-medre.md — config defaults correct                                   | ✅                                                  | No TOML references. Adapter blocks all use `adapter_kind: real`.                                                                                                                                                                                                                                                                                                                                     |
| docs/ops/troubleshooting.md — config error guidance current                           | ✅                                                  | No TOML references. Uses `adapter_kind: fake` and `adapter_kind: real` in examples.                                                                                                                                                                                                                                                                                                                  |
| docs/changes/unreleased.md — changelog fragment for this tranche                      | ❌                                                  | No entry for config-schema-authority-hardening. See F-022.                                                                                                                                                                                                                                                                                                                                           |
| tests/test_example_configs.py — what it validates                                     | ✅                                                  | Validates YAML parseability, real-secret scan, deprecated-language scan, storage backend, adapter*kind validity, runtime build for fake-bridge-smoke and fake-multi-adapter, structure assertions for docker-* and matrix.yaml and mixed-\_.yaml. Gap: 4 minimal configs not in the list (F-015).                                                                                                    |
| tests/test_config_runtime_parity.py — what parity it checks                           | ✅                                                  | Load → build parity for fake-bridge-smoke, fake-multi-adapter, and the two docker configs (with placeholder resolution). Disabled-adapter skip behavior, unknown-transport hard error, minimal config build. Gap: only 4 configs covered (F-016).                                                                                                                                                    |
| tests/test_docs_schema_examples.py — schema/example validation                        | ✅                                                  | Tests that every example validates against its schema, every required field is present in the example, and source drift detection for `CanonicalEvent`, `DeliveryReceipt`, `AdapterDeliveryResult`. Gap: does NOT do source drift detection for `RouteConfig` or the four adapter config dataclasses.                                                                                                |
| tests/test_docs_misc_consistency.py — what consistency checks                         | ✅                                                  | Covers: no private CLI imports, replay distinguishability, retry opt-in wording, stale trace `--config` references, config check exit code 2, docker-compose filename accuracy, source-tree examples wording, no `tcp_port` in examples, live config helper uses `port`, failure taxonomy naming. No config-schema-authority-specific checks.                                                        |
| tests/test_docs_single_authority.py — single authority checks                         | ✅                                                  | Guards "single source of truth" and "this contract defines" language to `docs/spec/` only. Not config-specific.                                                                                                                                                                                                                                                                                      |

## TOML Reference Inventory

Repo-wide grep for `medre\.toml`, `config\.toml`, `\.toml` across `src/`,
`docs/`, `examples/`, `tests/`. Each occurrence classified.

### src/

| File:line                                                             | Occurrence                                                                                         | Classification                                                                                                  |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `src/medre/config/loader.py:67`                                       | `_TOML_SUFFIX = ".toml"`                                                                           | ALLOWED (rejection constant).                                                                                   |
| `src/medre/config/loader.py:70-72`                                    | `_TOML_NOT_SUPPORTED_MSG = "TOML config files are no longer supported; use YAML (.yaml or .yml)."` | ALLOWED (the explicit rejection message).                                                                       |
| `src/medre/config/loader.py:81, 87-88, 90-91, 112, 121, 146-148, 248` | Docstrings and raise messages referencing `.toml` rejection                                        | ALLOWED (intentional rejection code paths and docs).                                                            |
| `src/medre/runtime/docker_bridge_artifacts.py:1263`                   | `for pattern in ("*.json", "*.log", "*.yaml", "*.yml", "*.toml"):`                                 | NOT ALLOWED (mild). Defensive artifact-collection glob that includes `.toml`. Does not load configs. See F-020. |

### docs/

| File:line                                                         | Occurrence                                                                            | Classification                      |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ----------------------------------- |
| `docs/ops/install.md:268`                                         | `pyproject.toml`                                                                      | ALLOWED (packaging metadata).       |
| `docs/ops/configuration.md:23`                                    | "rejects `.toml` with a clear error"                                                  | ALLOWED (documents the rejection).  |
| `docs/ops/diagnostics-and-evidence.md:387`                        | `pyproject.toml` (pytest config)                                                      | ALLOWED (tooling metadata).         |
| `docs/dev/release-readiness-audit.md:21-24`                       | `pyproject.toml` references in audit table                                            | ALLOWED (packaging metadata audit). |
| `docs/dev/testing.md:105, 380`                                    | `pyproject.toml` (pytest config, asyncio mode)                                        | ALLOWED (tooling metadata).         |
| `docs/dev/live-test-harness.md:28`                                | `pyproject.toml` (pytest config)                                                      | ALLOWED (tooling metadata).         |
| `docs/dev/yaml-config-migration-audit.md` (many lines)            | The migration audit doc — extensive `.toml` references documenting the YAML migration | ALLOWED per task brief.             |
| `docs/spec/configuration.md:181`                                  | "rejects `.toml` with a clear error"                                                  | ALLOWED (documents the rejection).  |
| `docs/changes/unreleased.md:369, 374, 395-396, 419, 425-426, 467` | Changelog entries documenting the YAML migration                                      | ALLOWED (historical changelog).     |

### examples/

| File:line | Occurrence                                                                                       | Classification |
| --------- | ------------------------------------------------------------------------------------------------ | -------------- |
| _(none)_  | No `.toml`, `medre.toml`, or `config.toml` references in `examples/configs/` or `examples/env/`. | ✅             |

### tests/

| File:line                                                                                                                                                                                          | Occurrence                                                                        | Classification                                                                                     |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `tests/test_config_yaml_discovery.py:111, 189, 193, 410, 414, 430, 432`                                                                                                                            | Legacy `.toml` discovery rejection tests                                          | ALLOWED (intentional rejection tests).                                                             |
| `tests/test_config_yaml_loader.py:374, 383, 394, 405, 416`                                                                                                                                         | Explicit `.toml` path rejection tests                                             | ALLOWED (intentional rejection tests).                                                             |
| `tests/test_config_env.py:916, 921`                                                                                                                                                                | `MEDRE_CONFIG=/etc/medre/medre.toml` as env-var parsing test data                 | NOT ALLOWED (mild). Misleading naming; file is never loaded. See F-019.                            |
| `tests/test_cli_install_metadata.py:279`                                                                                                                                                           | `/etc/medre/access_token_config.toml` in a redaction test fixture string          | ALLOWED (the path is test data for redaction, not a loaded config). Borderline — could be `.yaml`. |
| `tests/test_smoke_runtime_evidence.py:152`                                                                                                                                                         | `/nonexistent/config.toml` as a failure-path config                               | NOT ALLOWED (mild). Misleading extension. See F-018.                                               |
| `tests/test_runtime_evidence_completeness.py:88`                                                                                                                                                   | `/nonexistent/config.toml` as a failure-path config                               | NOT ALLOWED (mild). See F-018.                                                                     |
| `tests/test_replay_cancellation_shutdown.py:71`, `tests/test_replay_partial_failure.py:75`, `tests/test_persistence_authority_replay.py:79, 144, 244`, `tests/test_replay_bridge_conditions.py:72` | `config_file=tmp / "config" / "config.toml"` in `MedrePaths` fixture construction | NOT ALLOWED (mild). Misleading naming; path is never loaded. See F-017.                            |

## Test Coverage Gaps

Specific test cases that should be added, mapped to the test file they
belong in.

### `tests/test_example_configs.py`

- **TC-001**: Add `lxmf-receiver.yaml`, `lxmf-sender.yaml`,
  `meshcore-lab.yaml`, `meshcore-tbeam.yaml` to
  `REQUIRED_YAML_CONFIGS` (or a new `MINIMAL_CONFIGS` list). Pair with
  F-002 fix.
- **TC-002**: Add a parametrized `test_no_invalid_adapter_kind` that
  globs `examples/configs/*.yaml` and asserts every `adapter_kind`
  value is in `("real", "fake", None)`. Currently
  `test_adapter_kinds_valid` does this but only over
  `ALL_SHIPPED_CONFIGS` which excludes the four minimal configs.
- **TC-003**: Add a `TestLxmfReceiver` / `TestLxmfSender` /
  `TestMeshCoreLab` / `TestMeshCoreTBeam` class that runs
  `load_config()` against each (after F-002 fix) and asserts the
  expected adapter IDs and transport kinds. Pair with F-016.

### `tests/test_config_runtime_parity.py`

- **TC-004**: Extend `TestExampleConfigsUseSameLoader.ALL_CONFIGS` to
  include the four minimal configs after F-002 is fixed.
- **TC-005**: Add a `CREDENTIAL_REQUIRED_CONFIGS` list covering
  `matrix.yaml`, `meshtastic-serial.yaml`, `mixed-matrix-meshtastic.yaml`,
  `live-matrix-meshtastic.yaml`,
  `live-matrix-meshtastic-channel-map.yaml` and a parametrized test
  that asserts each raises the expected credential error class
  (`MatrixConfigError` for the Matrix ones,
  `LxmfConfigError`/`MeshCoreConfigError` for transports that require
  hardware).

### `tests/test_docs_schema_examples.py`

- **TC-006**: Add source drift detection for `RouteConfig` fields.
  Mirror the existing `test_canonical_event_schema_matches_source`
  pattern but iterate `dataclasses.fields(RouteConfig)` against the
  `RouteConfig` arm of `routing-config.schema.json`. This would have
  caught the missing `source_origin_label` / `dest_origin_label`
  documentation in the example.
- **TC-007**: Add source drift detection for the four adapter config
  dataclasses against `adapter-config.schema.json`. This would have
  caught F-001 (missing Meshtastic packet-routing fields) at the
  moment they were added.
- **TC-008**: Add a test that asserts each arm of the
  `adapter-config.schema.json` `oneOf` has at least one example
  payload in `docs/schemas/examples/` (currently only Matrix has one).
  Pair with F-011.
- **TC-009**: Add a test asserting that the structured
  `ChannelRoomMapEntry` shape appears in at least one example payload
  in `docs/schemas/examples/routing-config-example.json`. Pair with
  F-010.

### `tests/test_config_loader.py` (or a new `tests/test_config_unknown_keys.py`)

- **TC-010**: Test that an unknown root-level key (e.g. `roues:`)
  is either rejected with `ConfigValidationError(section_path="<root>")`
  OR is documented as silently dropped. Decision required (F-012).
- **TC-011**: Test that an unknown key in a route table (e.g.
  `routes.foo: { source_adapters: [...], bogusextra: 123 }`) is
  rejected with `ConfigValidationError(section_path="routes.foo")`.
  Pair with F-014 fix.
- **TC-012**: Test that an unknown key in an adapter table is handled
  consistently with the schema's `additionalProperties`. Pair with
  F-013 / F-021 decision.

### `tests/test_routes_channel_room_map_context_labels.py` (already covers most label edge cases)

- **TC-013**: Test that a `null` (explicit YAML `null`) per-entry
  `source_origin_label` falls back to the route-level label, then to
  the adapter `origin_label`. The file covers bool-rejected and
  unknown-key-rejected but does not have an explicit null-falls-back
  test.
- **TC-014**: Test that an explicit empty string per-entry
  `source_origin_label` suppresses the adapter-level fallback and the
  `{origin_label}` template variable renders empty. Pair with the
  spec text at §17.5.8 lines 1397-1401.

### `tests/test_config_yaml_strict.py` (already very thorough)

- No additional cases identified. The file covers duplicate keys,
  anchors, aliases, merge keys, custom tags, exotic mapping key types,
  multi-doc, non-mapping top-level, and secret redaction thoroughly.

## Intentionally Deferred

Items that are out of scope for this tranche, with reasoning.

- **`docs/dev/yaml-config-migration-audit.md` extensive TOML
  references.** Explicitly allowed by the task brief. This doc is the
  historical migration audit and is preserved as-is.
- **`pyproject.toml` references across docs and tests.** Packaging
  metadata, not runtime config. Always allowed.
- **`tests/integration/test_meshtasticd_sdk_bridge.py` and other
  Docker integration tests.** Excluded from the default suite by
  `-m 'not live and not docker'`. Not examined in detail for TOML
  references because they are opt-in and the audit scope is the
  default-runnable config surface.
- **`docs/dev/source-context-origin-label-audit.md` and
  `docs/dev/relay-prefix-attribution-audit.md`.** These are the prior
  tranche's audit docs and intentionally discuss the
  `matrix_relay_prefix` / `meshnet_name` removal history.
- **`docs/dev/release-readiness-audit.md` `pyproject.toml` table.**
  Audit of packaging metadata, not runtime config.
- **Test fixtures in `tests/helpers/` and `tests/fixtures/` that
  construct MatrixConfig/MeshtasticConfig directly.** These bypass
  the YAML loader entirely and are not in scope for the
  config/docs/examples authority audit. The audit is about what
  operators see (docs, examples, schemas) and what the loader enforces.
- **The `relay-prefix-attribution-audit.md` references to
  `_apply_matrix_relay_prefix` as a function name.** This is a
  renderer helper function name, not the removed config field. The
  function name is retained because it describes what the function
  does (apply the Matrix-local relay prefix).
- **Spec-level decision to keep `docs/spec/configuration.md` thinner
  than `docs/ops/configuration.md`.** The spec page may intentionally
  be a high-level summary while the ops page is the operator-canonical
  reference. F-005, F-006, and F-007 should still be fixed so the spec
  is not actively misleading, but the spec does not need to duplicate
  the ops page's exhaustive tables.
