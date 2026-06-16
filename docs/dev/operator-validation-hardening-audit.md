# Operator Validation Hardening Audit

Date: 2026-06-16
Branch: operator-validation-hardening

Static audit of MEDRE's operator-facing validation workflow. The prior
change (`config-schema-authority-audit.md`) hardened the config/schema/docs
authority chain: unknown-key rejection, strict YAML keys, section-type
validation, and the `filter_hooks` schema fix. This change asks whether that
authority chain holds up as a practical operator workflow: can an operator
trust `medre config check` as a pre-flight gate, are example configs checked
the same way runtime configs are, and are errors useful and secret-safe?

## Summary

MEDRE's config validation chain is fundamentally sound and significantly more
robust than typical pre-release projects. The strict YAML parser
(`src/medre/config/_yaml.py`) rejects every dangerous YAML feature (anchors,
aliases, merge keys, duplicate keys, exotic tags, multi-document streams)
with `path:line:column:` messages that never echo raw file content. The
loader (`src/medre/config/loader.py`) enforces unknown-key rejection at every
section boundary with deterministic, key-name-only error messages and
populated `section_path` attributes. `medre config check` catches the broad
`Exception` base at `src/medre/cli/config_commands.py:62`, renders a clean
`Config error: {exc}` message to stderr, and exits with code 2
(`EXIT_CONFIG`) — no tracebacks for normal config errors, verified by
`tests/test_cli_config_commands.py::TestConfigCheckErrors` and
`TestConfigCheckStrictValidation`.

All 15 shipped example configs in `examples/configs/` are covered by tests.
14 are in `REQUIRED_YAML_CONFIGS` and 1 (`docker-bridge-smoke.yaml`) is in
`PLACEHOLDER_CREDENTIAL_CONFIGS` at `tests/test_example_configs.py:30-52`.
Every config is at minimum YAML-parsed, secret-scanned, deprecated-language-
checked, storage-backend-validated, and `adapter_kind`-validated. 6 configs
go through the full `load_config → RuntimeBuilder.build()` path via
`tests/test_config_runtime_parity.py`; the remainder are tested for
deterministic load-time failures (credential errors, hardware-field errors,
or `${ENV_VAR}` placeholder errors) with assertions that the failure is the
_correct_ transport-specific error class, never a raw `ConfigValidationError`
or traceback.

The gaps this audit identifies are workflow-level, not authority-level. Three
findings block the operator pre-flight contract: (1) `medre config check`
does **not** call `validate_route_adapter_refs`, so a config with a route
referencing a nonexistent adapter passes `config check` with exit 0 and
"Config valid" but fails at `medre run` startup; (2) the Docker example
configs that use `${ENV_VAR}` syntax cannot be loaded by `medre config check`
at all — the loader's path-placeholder expander misinterprets `${VAR}` as a
path placeholder and raises `ConfigFileError`, an operator-confusing quirk
documented in tests but absent from operator docs; (3) `configuration.md`
mentions `medre config check` only in passing and never describes its output
shape, exit-code semantics, or what classes of errors it surfaces. Smaller
gaps include a stale `examples/configs/README.md` inventory (lists 11 of 15
configs), no `pre-commit-config.yaml` for local pre-commit validation, no
dedicated example-validation CI step, and a handful of low-severity
secret-leak patterns where non-credential config values are echoed in
validation errors.

## Methodology

Static read-only analysis of the config-loading chain, CLI entry points,
example configs, test suites, operator docs, and CI configuration. No tests
were executed. Every finding cites `file:line`. The audit cross-checks
claims in operator docs (`docs/ops/*.md`) against the test suite
(`tests/test_example_configs.py`, `tests/test_config_runtime_parity.py`,
`tests/test_cli_config_commands.py`, `tests/test_cli_diagnostics_commands.py`)
and against CI (`.github/workflows/test-and-coverage.yml`, `scripts/ci/`).

## Findings

### [F-001] examples/configs/README.md inventory is incomplete (11 of 15 configs listed)

- **Category**: stale wording
- **Location**: `examples/configs/README.md:6-18`
- **Current state**: The README table lists 11 configs (`fake-bridge-smoke`,
  `fake-multi-adapter`, `fake-retry-smoke`, `matrix`, `meshtastic-serial`,
  `live-matrix-meshtastic`, `live-matrix-meshtastic-channel-map`,
  `mixed-matrix-meshtastic`, `docker-matrix-bridge`,
  `docker-meshtastic-bridge`, `docker-bridge-smoke`). The four minimal
  single-adapter templates (`lxmf-receiver.yaml`, `lxmf-sender.yaml`,
  `meshcore-lab.yaml`, `meshcore-tbeam.yaml`) are absent despite shipping in
  the same directory and being in `REQUIRED_YAML_CONFIGS` at
  `tests/test_example_configs.py:42-45`.
- **Expected state**: README inventory matches the 15 configs present in
  `examples/configs/`. The four minimal templates should be described as
  "minimal templates requiring env-var completion" with a pointer to the
  `MEDRE_ADAPTER__<TOKEN>__<FIELD>` pattern shown in their inline comments
  (e.g. `examples/configs/lxmf-receiver.yaml:13-17`).
- **Recommendation**: Add four rows to the README table classifying the
  minimal configs as "Minimal template — env-var completion required" and
  note their intended use (single-adapter bring-up).

### [F-002] mixed-matrix-meshtastic.yaml marked "Superseded" but still first-class tested

- **Category**: stale wording
- **Location**: `examples/configs/README.md:15` (claim) vs
  `tests/test_example_configs.py:30-46` and
  `tests/test_config_runtime_parity.py:459` (test treatment)
- **Current state**: README describes `mixed-matrix-meshtastic.yaml` as
  "**Superseded by `live-matrix-meshtastic.yaml`.** Historical reference
  only." However the config remains in `REQUIRED_YAML_CONFIGS`, has a
  dedicated test class `TestMixedMatrixMeshtastic` at
  `tests/test_example_configs.py:310-367` that exercises YAML structure,
  route shape, and credential-error behavior, and appears in
  `CREDENTIAL_REQUIRED_CONFIGS` at
  `tests/test_config_runtime_parity.py:459` expecting `MatrixConfigError`.
- **Expected state**: Either (a) remove the "Superseded" framing and
  document it as a supported variant alongside the canonical
  `live-matrix-meshtastic.yaml`, or (b) move it out of
  `REQUIRED_YAML_CONFIGS` into a legacy/deprecated list and drop the
  runtime-parity assertion. The current state sends mixed signals: tests
  treat it as first-class, docs treat it as legacy.
- **Recommendation**: Clarify intent. If retained, update README to say
  "Variant retained for backward compatibility; prefer
  `live-matrix-meshtastic.yaml` for new deployments." If genuinely
  superseded, add a change fragment and remove from the required list in a
  follow-up.

### [F-003] No pre-commit configuration exists

- **Category**: missing CI/pre-commit coverage
- **Location**: repository root (no `.pre-commit-config.yaml` or
  `.pre-commit-config.yml` present)
- **Current state**: No pre-commit hook config exists. Example-config
  validation runs only in CI as part of the full pytest suite
  (`.github/workflows/test-and-coverage.yml:46-52`). A contributor who
  introduces a malformed example config or a `generate_sample_config()`
  regression will not discover it until CI runs the full suite.
- **Expected state**: A lightweight pre-commit hook (or a
  `scripts/ci/validate-examples.sh` script) that runs at least
  `medre config check` against every `examples/configs/*.yaml` and parses
  `generate_sample_config()` output, failing fast on the cheap checks
  before the contributor pushes.
- **Recommendation**: Add a `scripts/ci/validate-examples.sh` that iterates
  `examples/configs/*.yaml`, runs `parse_yaml_config` on each, and runs
  `load_config` on the fake-only configs. Wire it as a pre-commit hook or
  document it as a manual step in `docs/dev/testing.md`. The fake-only
  configs (`fake-bridge-smoke`, `fake-multi-adapter`, `fake-retry-smoke`,
  `docker-bridge-smoke`) can be fully load-validated in milliseconds; the
  credential/hardware configs can be YAML-validated only.

### [F-004] No dedicated example-validation CI step

- **Category**: missing CI/pre-commit coverage
- **Location**: `.github/workflows/test-and-coverage.yml:45-52`
- **Current state**: The CI workflow runs the full pytest suite with
  coverage in a single step. Example-config validation is a side effect of
  `tests/test_example_configs.py` and `tests/test_config_runtime_parity.py`
  running, both of which fall in the "Other" bucket of the slow-suite
  partition strategy (`docs/dev/testing.md:643`, "~1,900 tests,
  unmeasured"). A broken example config surfaces only after the full suite
  collects and dispatches these files.
- **Expected state**: A dedicated, fast CI step (or a labeled job) that
  runs only `tests/test_example_configs.py tests/test_config_runtime_parity.py
tests/test_cli_config_commands.py` so example-config regressions fail
  fast and visibly, separate from the broad test result.
- **Recommendation**: Either split a named CI step
  (`pytest tests/test_example_configs.py tests/test_config_runtime_parity.py
-q`) before the full suite, or document in
  `docs/dev/testing.md` that these two files are the canonical
  example-validation gate and should be run as a prefix slice during
  example-config changes.

### [F-005] Docker `${ENV_VAR}` configs cannot be loaded by `medre config check`

- **Category**: blocking operator workflow issue
- **Location**: `src/medre/config/loader.py:700-720` (`_expand_paths_in_dict`)
  vs `examples/configs/docker-matrix-bridge.yaml`,
  `examples/configs/docker-meshtastic-bridge.yaml`;
  behavior pinned by `tests/test_example_configs.py:1197-1277`
  (`TestDockerConfigsEnvVarValidation`)
- **Current state**: The Docker bridge configs use shell-style
  `${MEDRE_HOMESERVER}`, `${MEDRE_ACCESS_TOKEN}`, `${MESHTASTIC_HOST}` for
  Docker `env_file` injection. The loader's `_expand_paths_in_dict` treats
  `{...}` (any brace-delimited token) as a path placeholder. When the
  parser encounters `${MEDRE_HOMESERVER}`, it sees `{MEDRE_HOMESERVER}` as
  a path-placeholder token, fails to recognise `MEDRE_HOMESERVER` as a
  known placeholder (only `{config}`, `{state}`, `{data}`, `{cache}`,
  `{logs}` are valid), and raises `ConfigFileError("Invalid path
placeholder in config field 'homeserver': ...")` via
  `src/medre/config/loader.py:707-710`. An operator who copies
  `docker-matrix-bridge.yaml` and runs `medre config check --config ...`
  receives a "placeholder" error that mentions neither Docker nor the env
  vars they are supposed to set. The tests
  (`TestDockerConfigsEnvVarValidation`) pin this behavior as correct and
  assert the error message is clean, but they do not make it operator-
  discoverable.
- **Expected state**: Either (a) the loader recognises `${VAR}` as a
  distinct syntax from `{placeholder}` and defers resolution to the
  container runtime (preferred, larger change), or (b) the example configs
  and operator docs explicitly warn that Docker `${VAR}` configs require
  pre-resolution before `medre config check`/`medre run` can load them,
  and point to `tests/test_config_runtime_parity.py:47-59`
  (`_resolve_docker_placeholders`) as the reference pattern.
- **Recommendation**: In the short term, add a "Loading Docker example
  configs" subsection to `docs/ops/configuration.md` and
  `examples/configs/README.md` that documents the `${VAR}` limitation and
  the env-var resolution requirement. In the long term, consider teaching
  the loader to distinguish `${VAR}` (deferred env reference) from
  `{placeholder}` (path expansion) so `medre config check` works uniformly.

### [F-006] docs/ops/configuration.md does not document the `${ENV_VAR}` loading limitation

- **Category**: stale wording / blocking operator workflow (pairs with F-005)
- **Location**: `docs/ops/configuration.md` (whole document); Docker
  examples at `docs/ops/configuration.md:917-943` use `.env` files but do
  not address direct `medre config check` behavior
- **Current state**: The configuration reference shows Docker `.env` file
  usage (line 917+) and environment-variable overrides (line 563+) but
  never warns that a YAML config embedding literal `${VAR}` strings cannot
  be loaded by `medre config check` or `medre run`. The only place this
  limitation is documented is inside the test docstrings at
  `tests/test_example_configs.py:816-824` and
  `tests/test_config_runtime_parity.py:47-59`, which operators do not read.
- **Expected state**: A clear note in the "Configuration Search Order" or
  "Path Placeholders" section explaining that `{name}` is a path-
  placeholder syntax, that `${NAME}` is _not_ a MEDRE-supported
  interpolation, and that Docker configs using `${NAME}` must be
  pre-resolved (or use the `MEDRE_ADAPTER__<TOKEN>__<FIELD>` env-override
  pattern instead).
- **Recommendation**: Add a short subsection titled "Path placeholders vs
  env-var references" to `docs/ops/configuration.md` after the existing
  "Path Placeholders" section (line 537+).

### [F-007] docs/ops/configuration.md pre-flight guidance for `medre config check` is thin

- **Category**: stale wording
- **Location**: `docs/ops/configuration.md:26` (single sentence) and
  `docs/ops/configuration.md:857-858` (CLI command listing)
- **Current state**: The configuration reference mentions `medre config
check` twice: line 26 ("Use `medre config check` to verify which file is
  loaded and whether it parses correctly.") and the CLI command block at
  line 857 ("`medre config check [--config PATH]` — Load and validate
  config. Exits with code 2 on errors."). Neither describes what the
  output looks like (adapter inventory, route inventory, runtime limits,
  startup preview), what classes of errors are caught (YAML parse, unknown
  keys, type errors, adapter `validate()` failures, runtime.limits range
  checks), what is _not_ caught (route adapter-ref validation — see
  F-016), or how to interpret the "Config has N error(s)" summary block at
  `src/medre/cli/config_commands.py:184-186`.
- **Expected state**: A dedicated "Pre-flight validation with `medre
config check`" subsection describing: (a) what the command does (load +
  per-adapter `validate()` + `RuntimeLimits.validate()` + summary), (b)
  what exit codes mean (0 = valid, 2 = config error), (c) what errors look
  like (single-line `Config error: ...` on stderr, no traceback), (d) what
  it does _not_ check (route adapter refs, SDK availability, storage path
  writability — all deferred to `medre run`), and (e) the relationship to
  `medre routes validate` which covers the route-adapter-ref gap.
- **Recommendation**: Add the subsection. Cross-link to
  `docs/ops/troubleshooting.md` "Config Failure Drills" (line 28+) which
  already has excellent drill-style coverage of bad YAML, unknown adapter
  refs, and duplicate route IDs.

### [F-008] docs/ops/troubleshooting.md "Config Failure Drills" section is strong (no gap, noted for coverage)

- **Category**: intentionally deferred
- **Location**: `docs/ops/troubleshooting.md:28-93`
- **Current state**: The troubleshooting guide has dedicated drills for
  bad YAML syntax (`/tmp/bad-syntax.yaml`), unknown adapter refs in routes
  (`/tmp/bad-route.yaml` → `medre routes validate`), and a `medre smoke
--drill bad_route_config` drill that exits 0 when the expected error is
  caught. Each drill states the expected exit code (2), the expected
  error class, and the fix. This is the strongest part of the operator
  validation story.
- **Expected state**: No change required. Noted as positive coverage.
- **Recommendation**: Use this drill pattern as the template when adding
  drills for the F-016 gap (route adapter refs not checked by
  `medre config check`) and the F-005 gap (Docker `${VAR}` configs).

### [F-009] ConfigValidationError structured attributes are not rendered by `__str__`

- **Category**: intentionally deferred
- **Location**: `src/medre/config/errors.py:16-42`,
  `src/medre/cli/config_commands.py:63`
- **Current state**: `ConfigValidationError` carries `transport`,
  `adapter_id`, and `section_path` keyword attributes (lines 35-41). The
  CLI renders the exception via `f"Config error: {exc}"` (line 63), which
  calls `Exception.__str__` → returns only the `message` positional arg.
  The structured attributes are invisible to the operator unless the
  message text itself embeds them (which the loader does consistently —
  e.g. `loader.py:154` embeds `section_path` in the message text). The
  attributes are therefore only useful to programmatic callers that catch
  the exception and inspect `.section_path`.
- **Expected state**: No change required for the CLI path. If future
  tooling (e.g. a structured `medre config check --json` output) needs the
  attributes, they are already available. The current single-line stderr
  rendering is appropriate for terminal operators.
- **Recommendation**: Defer. If a JSON output mode is added to
  `medre config check`, render the structured attributes there.

### [F-010] routes.py policy validation echoes non-credential values in error messages

- **Category**: secret-leak risk (low)
- **Location**: `src/medre/config/routes.py:179` (`f"...Did you mean
[{raw!r}]?"`), `src/medre/config/routes.py:192` (`f"...got
{type(item).__name__}: {item!r}"`)
- **Current state**: When a route policy field (`sender_allowlist`,
  `room_allowlist`, `channel_allowlist`, `allowed_source_adapters`,
  `allowed_dest_adapters`, `allowed_event_types`) is provided as a bare
  string instead of a list, the error at line 179 echoes the string value:
  `"...must be a list, not a string. Did you mean ['<value>']?"`. When a
  list element is not a string, line 192 echoes the element value:
  `"...must be a string, got <type>: <value>"`. These are policy fields,
  not credential fields, but `sender_allowlist` could contain user
  identifiers an operator considers sensitive, and `room_allowlist` could
  contain private room IDs.
- **Expected state**: Echoing allowlist _values_ in errors is useful for
  actionable diagnostics and the fields are not credentials, so the
  trade-off favors keeping the echo. The risk is that an operator who
  mistakenly puts a token in `sender_allowlist` (e.g. copy-paste error)
  would see it echoed in the error and potentially in logs.
- **Recommendation**: Acceptable as-is for non-credential fields. If
  defense-in-depth is desired, truncate echoed values to a bounded length
  (e.g. `raw[:32] + '...' if len(raw) > 32 else raw`) so a misplaced long
  secret is not fully echoed. Low priority.

### [F-011] env.py coercion errors echo the raw env-var value

- **Category**: secret-leak risk (low)
- **Location**: `src/medre/config/env.py:158` (`f"...got {raw!r}"`),
  `src/medre/config/env.py:171` (`f"...got {raw!r}"`),
  `src/medre/config/env.py:184` (`f"...got {raw!r}"`)
- **Current state**: `_coerce_bool`, `_coerce_int`, and `_coerce_float`
  echo the raw env-var value (`{raw!r}`) when coercion fails. The env-var
  _name_ is also echoed (`{env_name!r}`). These coercers run when an
  adapter field has a `bool`/`int`/`float` type hint and the operator
  provides an uncoercible string. Secret-bearing fields (`access_token`,
  `identity_path`, `password`) are typed as `str` and never hit these
  coercers, so the direct risk is low. The indirect risk: if a future
  field is mis-typed (e.g. a `port: int` field accidentally fed a token),
  the value would leak in the error.
- **Expected state**: Coercion-failure messages should name the env var
  and the expected type without echoing the value, _or_ should echo the
  value only when the env-var name does not match the secret-key patterns
  (`TOKEN`, `SECRET`, `PASSWORD`, `KEY`, `AUTH`, `CREDENTIAL`) already
  enumerated at `docs/ops/configuration.md:821`.
- **Recommendation**: Add a guard in `_coerce_bool`/`_coerce_int`/
  `_coerce_float` that redacts `{raw!r}` when `env_name.upper()` matches
  the secret-key pattern. Low priority because secret fields are typed
  `str` today, but cheap defense-in-depth.

### [F-012] loader.py retry/logging validation echoes non-credential values

- **Category**: secret-leak risk (very low)
- **Location**: `src/medre/config/loader.py:525,541,557` (retry fields),
  `src/medre/config/loader.py:590,597,606,613,639` (logging fields)
- **Current state**: The retry and logging validators echo `{raw!r}` for
  field values that fail type/range/enum checks. The fields involved are
  `interval_seconds`, `batch_size`, `max_attempts`, `enabled` (retry
  section) and `level`, `format`, `overrides` values (logging section).
  None of these are credential fields. The echoed values are numbers,
  booleans, or log-level strings.
- **Expected state**: No change required. The echoed values are non-secret
  configuration scalars and the echo improves diagnostics.
- **Recommendation**: Defer. No action needed.

### [F-013] MatrixConfig validation echoes homeserver, user_id, encryption_mode, and auto_join_rooms values

- **Category**: secret-leak risk (low, by design)
- **Location**: `src/medre/config/adapters/matrix.py:154` (`homeserver`),
  `:160` (`user_id`), `:183` (`auto_join_rooms` entry), `:191`
  (`encryption_mode`)
- **Current state**: `MatrixConfig._validate_fields` echoes the offending
  value for `homeserver` (`got {self.homeserver!r}`), `user_id` (`got
{self.user_id!r}`), `auto_join_rooms` entries (`got {entry!r}`), and
  `encryption_mode` (`got {self.encryption_mode!r}`). The `access_token`
  field is deliberately _not_ echoed — line 163 raises
  `"access_token must be non-empty"` without including the value, and the
  `__repr__` at lines 224-237 redacts the token to a 3-character preview.
  `homeserver` and `user_id` are not credentials but reveal deployment
  details (the homeserver URL and bot MXID).
- **Expected state**: Echoing `homeserver`/`user_id` is useful for
  actionable diagnostics (an operator with a typo in the homeserver URL
  benefits from seeing the typo echoed). The `access_token` non-echo is
  correct. The trade-off favors keeping the echo for these semi-sensitive
  fields.
- **Recommendation**: Acceptable as-is. The `access_token` handling is
  the correct pattern and is preserved. No action needed.

### [F-014] MatrixConfig.**repr** partial redaction; other adapter configs have no **repr**

- **Category**: intentionally deferred
- **Location**: `src/medre/config/adapters/matrix.py:224-237` (MatrixConfig
  has `__repr__`); `src/medre/config/adapters/meshtastic.py`,
  `meshcore.py`, `lxmf.py` (no `__repr__` defined)
- **Current state**: `MatrixConfig.__repr__` redacts `access_token` to
  `self.access_token[:3] + "…"` (line 226-228), matching the
  3-character token preview documented at `docs/ops/configuration.md:803`. The
  remaining MatrixConfig fields (`adapter_id`, `homeserver`, `user_id`,
  `room_allowlist`, `auto_join_rooms`) are echoed in full in the repr.
  `MeshtasticConfig`, `MeshCoreConfig`, and `LxmfConfig` inherit the
  default `dataclass.__repr__`, which echoes all fields. These configs
  carry no traditional credentials (Meshtastic/MeshCore connection params
  are network addresses; LXMF `identity_path` is a filesystem path, not
  the key material itself).
- **Expected state**: No change required for the current field set. If a
  future adapter field carries a credential (e.g. a MeshCore pre-shared
  key), that config class will need a custom `__repr__` mirroring the
  MatrixConfig pattern.
- **Recommendation**: Defer. The `configuration.md:823` note that
  "MeshCore `node_config` rejects keys named `private_key`, `secret`, or
  `password` at validation time" (verified at
  `src/medre/config/adapters/meshcore.py:268-271`) provides defense at the
  validation layer even without a `__repr__` override.

### [F-015] Strict YAML `_SECRET_KEY_NAMES` redaction is narrow but value-safe

- **Category**: intentionally deferred
- **Location**: `src/medre/config/_yaml.py:186-210` (`_SECRET_KEY_NAMES`,
  `_redact_key`)
- **Current state**: The strict YAML parser redacts _key names_ (not
  values) that match a secret pattern (`access_token`, `password`,
  `secret`, `api_key`, `apikey`, `token`, `private_key`, `client_secret`)
  in duplicate-key error messages at line 147. The redaction is narrow:
  it does not cover `identity_path` (LXMF) or `store_path` (Matrix crypto
  store). However, duplicate-key errors _only_ echo the key name, never
  the value (the parser raises before values are formatted, per the
  module docstring at lines 27-29), so the redaction is about avoiding
  confusion ("which key is duplicated?") not about value leakage.
- **Expected state**: No change required. The value-safety property
  (never echo raw file content in YAML errors) is enforced by
  `_format_mark` at lines 218-229 and `_sanitize_yaml_error` at lines
  232-260, both of which deliberately omit the buffer snippet that
  PyYAML normally includes.
- **Recommendation**: Defer. The narrow `_SECRET_KEY_NAMES` could be
  expanded to include `identity_path` for consistency, but the omission
  has no security impact because values are never echoed.

### [F-016] `medre config check` does not validate route adapter references

- **Category**: blocking operator workflow issue
- **Location**: `src/medre/cli/config_commands.py:55-211` (`_config_check`)
  — notably absent is any call to `validate_route_adapter_refs`;
  route inventory is printed at lines 157-180 but not validated
- **Current state**: `_config_check` calls `load_config` (line 61),
  iterates adapters to call `adapter_conf.validate()` (lines 102-108),
  calls `limits.validate()` (lines 152-155), and prints a route inventory
  (lines 157-180). It does **not** call the route-adapter-ref validation
  that `medre run` and `medre routes validate` perform. Consequently, a
  config with `routes.foo.dest_adapters: [nonexistent_adapter]` passes
  `medre config check` with exit 0 and the message "Config valid", then
  fails at `medre run` startup with `RouteValidationError`. The
  troubleshooting guide at `docs/ops/troubleshooting.md:44-74` correctly
  directs operators to `medre routes validate` for this class of error,
  but an operator who runs only `medre config check` as a pre-flight gate
  will receive a false-positive "valid" result.
- **Expected state**: Either `medre config check` should call
  `validate_route_adapter_refs` against the assembled adapter IDs and
  surface failures as validation errors (preferred — makes `config check`
  a complete pre-flight gate), or the "Config valid" output should
  explicitly note "route adapter references not checked — run `medre
routes validate`" so operators know the gate is incomplete.
- **Recommendation**: Add a route-adapter-ref check to `_config_check`
  after the route inventory is printed. The check needs the set of
  configured adapter IDs (already available via
  `config.adapters.all_configs()`) and the routes (already available via
  `config.routes.routes`). On failure, append to `validation_errors` so
  the existing exit-code logic at lines 184-186 applies. This closes the
  pre-flight gap and makes `medre config check` a trustworthy gate.

### [F-017] No test for `medre config check` exit code on adapter `validate()` failures

- **Category**: missing test coverage
- **Location**: `tests/test_cli_config_commands.py:161-195`
  (`TestConfigCheckErrors`)
- **Current state**: `TestConfigCheckErrors` covers: missing config file
  (exit nonzero), missing file clear message (no traceback), bad limits
  (exit nonzero + error shown), valid config (exit zero, "Config valid"
  in output). It does _not_ cover the adapter-`validate()`-failure path
  at `src/medre/cli/config_commands.py:102-108,184-186`, where an adapter
  config object's `validate()` method raises (e.g.
  `MatrixConfigError("homeserver must start with 'http://'")`) and the
  CLI appends it to `validation_errors`, prints "Config has N error(s)",
  and exits with `EXIT_CONFIG`. The `runtime.limits.validate()` path at
  lines 152-155 is covered by the `test_bad_limits_*` tests, but the
  per-adapter `validate()` path is not.
- **Expected state**: A test that constructs a config with an adapter
  whose `validate()` raises (e.g. a Matrix adapter with a malformed
  `homeserver`), runs `medre config check --config ...`, and asserts exit
  code 2 plus the validation-error message in output. This pins the
  behavior at lines 102-108 (adapter validation loop) and 184-186
  (error-count-driven exit).
- **Recommendation**: Add a `test_adapter_validate_failure_exits_nonzero`
  test to `TestConfigCheckErrors` using a config with a real Matrix
  adapter (`adapter_kind: real`) that has a non-empty but malformed
  `homeserver` (e.g. `homeserver: "not-a-url"`), so `MatrixConfig.validate()`
  raises `MatrixConfigError("homeserver must start with 'http://'...")`
  and the CLI surfaces it.

### [F-018] Sample config does not mention removed keys for migration awareness

- **Category**: stale wording (minor)
- **Location**: `src/medre/config/sample.py:20-257` (`generate_sample_config`)
- **Current state**: The sample config documents all currently-accepted
  sections and fields with inline comments. It does not mention keys that
  were removed or renamed by prior changes (e.g. keys now rejected by
  the unknown-key enforcement at `loader.py:140-158,444-451,477-484`).
  Operators migrating from an older MEDRE config receive
  `ConfigValidationError("unknown key(s) ...")` and must read the
  "Accepted keys:" list in the error to discover what changed. The sample
  does mark `device_id` and `store_path` as "internal/test only"
  (`sample.py:76-78`) but does not enumerate other removed keys.
- **Expected state**: The sample is a forward-looking reference, not a
  migration guide, so enumerating every removed key is out of scope.
  However, a one-line comment pointing operators to the change fragments
  under `docs/changes/` for migration context would help.
- **Recommendation**: Add a header comment to `generate_sample_config()`
  output pointing to `docs/changes/` for the change history. Low priority;
  the unknown-key error messages are already self-describing.

## Coverage Matrix

| Audit area                                                                       | Status      | Notes                                                                                                                                                                                                                              |
| -------------------------------------------------------------------------------- | ----------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `medre config check` invokes `load_config`                                       | ✅ Covered  | `config_commands.py:61`; `load_config` at `loader.py:312`                                                                                                                                                                          |
| `medre config check` catches normal config errors without traceback              | ✅ Covered  | Broad `except Exception` at `config_commands.py:62`; `print(f"Config error: {exc}", file=sys.stderr)` + `sys.exit(EXIT_CONFIG)` at `:63-64`. Verified by `test_cli_config_commands.py:170-176` (asserts "Traceback" not in stderr) |
| `medre config check` renders errors to stderr                                    | ✅ Covered  | `config_commands.py:63` writes to `sys.stderr`                                                                                                                                                                                     |
| `medre config check` exit code on config error                                   | ✅ Covered  | `EXIT_CONFIG` (2) at `config_commands.py:64`; verified by `TestConfigCheckErrors::test_missing_config_file`, `test_bad_limits_exits_nonzero`                                                                                       |
| `medre config check` exit code on adapter `validate()` failure                   | ⚠️ Partial  | Code path exists (`config_commands.py:102-108,184-186`) but no test pins it — see F-017                                                                                                                                            |
| `medre config check` validates route adapter refs                                | ❌ Missing  | `_config_check` does not call `validate_route_adapter_refs`; false "Config valid" possible — see F-016                                                                                                                             |
| `medre config check` runs `RuntimeLimits.validate()`                             | ✅ Covered  | `config_commands.py:152-155`; tested by `test_bad_limits_*`                                                                                                                                                                        |
| `medre config sample` produces parseable YAML                                    | ✅ Covered  | `test_cli_config_commands.py::test_sample_parses_as_yaml` (line 284), `test_config_loader.py:460-490`                                                                                                                              |
| `medre config sample` loads via `load_config`                                    | ✅ Covered  | `TestSampleConfigFakeBuildable::test_sample_loads_via_config_loader` (line 212)                                                                                                                                                    |
| `medre config sample` builds via `RuntimeBuilder`                                | ✅ Covered  | `TestSampleConfigFakeBuildable::test_sample_builds_via_runtime_builder` (line 233)                                                                                                                                                 |
| `medre config sample` demonstrates structured `channel_room_map`                 | ✅ Covered  | `TestSampleConfigStructuredChannelRoomMap` (line 495) asserts `channel_room_map`, `source_origin_label`, `dest_origin_label` appear in sample output                                                                               |
| `medre config sample` passes `medre config check`                                | ✅ Covered  | `test_sample_config_check_passes` (line 262) asserts "Config valid" in output                                                                                                                                                      |
| `medre config sample` mentions removed keys                                      | ❌ Missing  | No removed-key documentation in sample output — see F-018                                                                                                                                                                          |
| Loader error types (ConfigValidationError, ConfigFileError, ConfigNotFoundError) | ✅ Covered  | `errors.py:8-46`; three-class hierarchy with `ConfigError` base                                                                                                                                                                    |
| Loader error attributes (section_path, transport, adapter_id)                    | ✅ Defined  | `errors.py:35-41`; populated by loader at every raise site                                                                                                                                                                         |
| Loader error attributes rendered by `__str__`                                    | ⚠️ Deferred | `__str__` returns message only; structured attrs invisible to CLI — see F-009                                                                                                                                                      |
| Loader error messages include key NAMES                                          | ✅ Covered  | `_reject_unknown_keys` at `loader.py:140-158` uses `repr(k)` for key names                                                                                                                                                         |
| Loader error messages include VALUES                                             | ⚠️ Bounded  | Retry/logging validators echo `{raw!r}` (non-credential scalars) — see F-012                                                                                                                                                       |
| Loader error messages include section_path                                       | ✅ Covered  | Every `ConfigValidationError` raise in `loader.py` passes `section_path=...`                                                                                                                                                       |
| Loader error messages include accepted/valid keys                                | ✅ Covered  | `_reject_unknown_keys` appends `Accepted keys: {sorted(known)}` at `:156`                                                                                                                                                          |
| Strict YAML rejects anchors/aliases/merge keys/duplicate keys                    | ✅ Covered  | `_yaml.py:84-154`; scanner + constructor overrides                                                                                                                                                                                 |
| Strict YAML error messages name the rejected feature                             | ✅ Covered  | "YAML anchors (&) are not supported" (`:87`), "YAML aliases (\*)" (`:92`), "YAML merge keys (<<)" (`:122`), "duplicate mapping key" (`:147`)                                                                                       |
| Strict YAML error messages include path:line:column                              | ✅ Covered  | `_format_mark` at `_yaml.py:218-229`; verified by `test_cli_config_commands.py:323-340`                                                                                                                                            |
| Strict YAML never echoes raw file content                                        | ✅ Covered  | Module docstring `_yaml.py:27-29`; `_sanitize_yaml_error` at `:232-260` strips buffer snippets                                                                                                                                     |
| Unknown-key handling consistent across loader/routes/model                       | ✅ Covered  | Same `_reject_unknown_keys` pattern in `loader.py` and equivalent in `routes.py:161-169,304-313,573-581,1098-1102`                                                                                                                 |
| MatrixConfig `__repr__` redacts access_token                                     | ✅ Covered  | `matrix.py:224-237`; redacts to 3-char preview. Other adapters have no `__repr__` — see F-014                                                                                                                                      |
| Secret-leak grep: `{raw!r}` / `{raw}` in error messages                          | ⚠️ Bounded  | Found in `loader.py` (retry/logging, non-credential), `env.py` (coercion), `routes.py:179,192` (policy fields) — see F-010, F-011, F-012, F-013                                                                                    |
| Example configs: YAML parse via strict loader                                    | ✅ Covered  | `TestYamlParseable` at `test_example_configs.py:121-128` parametrized over `REQUIRED_YAML_CONFIGS`                                                                                                                                 |
| Example configs: load via `load_config`                                          | ✅ Covered  | Fake-only configs load fully; credential configs assert the expected failure class — see inventory below                                                                                                                           |
| Example configs: full `load_config → RuntimeBuilder.build()`                     | ✅ Covered  | `test_config_runtime_parity.py::TestExampleConfigsUseSameLoader` (4 configs) + per-config deep tests                                                                                                                               |
| Example configs: credential-required handling                                    | ✅ Covered  | `CREDENTIAL_REQUIRED_CONFIGS` at `test_config_runtime_parity.py:456-462` asserts transport-specific error class, never `ConfigValidationError`                                                                                     |
| Example configs: minimal-template handling                                       | ✅ Covered  | `MINIMAL_CONFIGS` at `test_config_runtime_parity.py:446-451` asserts `LxmfConfigError`/`MeshCoreConfigError`, never `ConfigValidationError`                                                                                        |
| Example configs: secret scanning                                                 | ✅ Covered  | `TestExampleHygiene::test_no_real_secrets` at `test_example_configs.py:580-583` parametrized over `ALL_SHIPPED_CONFIGS`                                                                                                            |
| Example configs: deprecated-language scanning                                    | ✅ Covered  | `TestExampleHygiene::test_no_deprecated_language` at `:586-589`                                                                                                                                                                    |
| Example configs: env var documentation cross-check                               | ✅ Covered  | `TestEnvVarDocumentation` at `:983-1058`                                                                                                                                                                                           |
| Example configs: Docker `${VAR}` clean-error behavior                            | ✅ Covered  | `TestDockerConfigsEnvVarValidation` at `:1197-1277` — but operator docs do not warn — see F-005                                                                                                                                    |
| `docs/ops/configuration.md` mentions `medre config check`                        | ⚠️ Thin     | Two passing mentions (lines 26, 857); no dedicated section — see F-007                                                                                                                                                             |
| `docs/ops/configuration.md` documents `${VAR}` limitation                        | ❌ Missing  | No warning that Docker `${VAR}` configs fail `load_config` — see F-006                                                                                                                                                             |
| `docs/ops/running-medre.md` mentions config validation                           | ✅ Covered  | Line 13-17: "To verify config without starting: `medre config check`"; exit-code table at `:60-79`                                                                                                                                 |
| `docs/ops/troubleshooting.md` config-error interpretation                        | ✅ Strong   | "Config Failure Drills" at `:28-93` with exit codes, drills, and `medre smoke --drill` commands — see F-008                                                                                                                        |
| CI validates example configs                                                     | ⚠️ Indirect | Full pytest run only; no dedicated step — see F-004                                                                                                                                                                                |
| Pre-commit config exists                                                         | ❌ Missing  | No `.pre-commit-config.yaml` — see F-003                                                                                                                                                                                           |
| `scripts/ci` pre-flight validation script                                        | ❌ Missing  | `scripts/ci/` has only Docker scripts — see F-003                                                                                                                                                                                  |
| Example validation tests run in default pytest tier (not gated)                  | ✅ Covered  | `test_example_configs.py` and `test_config_runtime_parity.py` have no `@pytest.mark.docker`/`live` decorators                                                                                                                      |
| Docs-operator path consistency                                                   | ⚠️ Gap      | Docs describe `medre routes validate` for route refs; `medre config check` does not cover it — see F-016                                                                                                                           |

## Example Config Inventory

15 configs in `examples/configs/`. "Loads via load_config" means the config
can be passed to `load_config()` and return a `RuntimeConfig`; configs that
intentionally fail loading assert a specific error class instead.

| Config                                    | Parses YAML | Loads via `load_config`                            | Requires credentials/hardware    | In REQUIRED list                         | Test coverage                                                                                                                                                                        |
| ----------------------------------------- | ----------- | -------------------------------------------------- | -------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `fake-bridge-smoke.yaml`                  | ✅          | ✅ (full)                                          | No                               | ✅                                       | Deep: `TestFakeBridgeSmoke`, `TestFakeBridgeSmokeDeep`, `TestFakeBridgeSmokeTwoWayRoutes`, `TestFakeConfigBuildsRuntime`, `TestFakeConfigRouteValidate`, `TestFakeConfigRuntimePath` |
| `fake-multi-adapter.yaml`                 | ✅          | ✅ (full)                                          | No                               | ✅                                       | Deep: `TestFakeMultiAdapter` (load + build + 4 fake adapters + routes)                                                                                                               |
| `fake-retry-smoke.yaml`                   | ✅          | ✅ (full)                                          | No                               | ✅                                       | Deep: `TestFakeRetrySmoke` (retry section + route retry policies)                                                                                                                    |
| `matrix.yaml`                             | ✅          | ❌ (`MatrixConfigError` on empty `access_token`)   | Matrix homeserver                | ✅                                       | `TestMatrixConfig::test_load_raises_credential_error`, `CREDENTIAL_REQUIRED_CONFIGS`                                                                                                 |
| `meshtastic-serial.yaml`                  | ✅          | ✅ (loads; hardware check at build)                | Meshtastic radio (serial)        | ✅                                       | `TestMeshtasticSerial`, `CREDENTIAL_REQUIRED_CONFIGS` (expected_error=None — loads OK)                                                                                               |
| `mixed-matrix-meshtastic.yaml`            | ✅          | ❌ (`MatrixConfigError` on empty `access_token`)   | Matrix + Meshtastic              | ✅                                       | `TestMixedMatrixMeshtastic`, `CREDENTIAL_REQUIRED_CONFIGS`. README marks "Superseded" — see F-002                                                                                    |
| `live-matrix-meshtastic.yaml`             | ✅          | ❌ (`MatrixConfigError`)                           | Matrix + Meshtastic              | ✅                                       | `TestLiveMatrixMeshtasticTargeting`, `CREDENTIAL_REQUIRED_CONFIGS`                                                                                                                   |
| `live-matrix-meshtastic-channel-map.yaml` | ✅          | ❌ (`MatrixConfigError`)                           | Matrix + Meshtastic              | ✅                                       | `CREDENTIAL_REQUIRED_CONFIGS` (credential-error path)                                                                                                                                |
| `docker-matrix-bridge.yaml`               | ✅          | ❌ (`ConfigFileError` on `${VAR}` placeholder)     | Docker Synapse                   | ✅                                       | `TestDockerMatrixBridgeConfig` (YAML structure only), `TestDockerConfigsEnvVarValidation` (clean error). Cannot load — see F-005                                                     |
| `docker-meshtastic-bridge.yaml`           | ✅          | ❌ (`ConfigFileError` on `${VAR}` placeholder)     | Docker meshtasticd               | ✅                                       | `TestDockerMeshtasticBridgeConfig` (YAML structure only), `TestDockerConfigsEnvVarValidation`. Cannot load — see F-005                                                               |
| `docker-bridge-smoke.yaml`                | ✅          | ✅ (placeholder token `CHANGE_ME` is non-empty)    | Docker Synapse + meshtasticd     | ❌ (in `PLACEHOLDER_CREDENTIAL_CONFIGS`) | `TestDockerBridgeSmoke` (YAML structure + loads with placeholder creds)                                                                                                              |
| `lxmf-receiver.yaml`                      | ✅          | ❌ (`LxmfConfigError` — missing `storage_path`)    | LXMF hardware (env-required)     | ✅                                       | `MINIMAL_CONFIGS` at `test_config_runtime_parity.py:446-451` asserts transport-specific error. Not in README — see F-001                                                             |
| `lxmf-sender.yaml`                        | ✅          | ❌ (`LxmfConfigError`)                             | LXMF hardware (env-required)     | ✅                                       | `MINIMAL_CONFIGS`. Not in README — see F-001                                                                                                                                         |
| `meshcore-lab.yaml`                       | ✅          | ❌ (`MeshCoreConfigError` — missing `host`)        | MeshCore hardware (env-required) | ✅                                       | `MINIMAL_CONFIGS`. Not in README — see F-001                                                                                                                                         |
| `meshcore-tbeam.yaml`                     | ✅          | ❌ (`MeshCoreConfigError` — missing `ble_address`) | MeshCore hardware (env-required) | ✅                                       | `MINIMAL_CONFIGS`. Not in README — see F-001                                                                                                                                         |

Notes on the inventory:

- The 4 minimal templates (`lxmf-receiver`, `lxmf-sender`, `meshcore-lab`,
  `meshcore-tbeam`) intentionally use `adapter_kind: real` with hardware
  fields commented out, to be completed via
  `MEDRE_ADAPTER__<TOKEN>__<FIELD>` env vars. They fail loading with the
  transport-specific config error (`LxmfConfigError`/`MeshCoreConfigError`),
  never with `ConfigValidationError` for the `adapter_kind` field. This is
  the F-002 fix referenced in
  `tests/test_config_runtime_parity.py:474-489`.
- The 2 Docker `${VAR}` configs (`docker-matrix-bridge`,
  `docker-meshtastic-bridge`) are validated for YAML structure and clean
  error behavior only. They cannot be loaded by `medre config check`
  because the loader treats `${VAR}` as a path placeholder. The
  `test_config_runtime_parity.py::_resolve_docker_placeholders` helper
  (lines 47-59) pre-resolves the placeholders so these configs _can_ be
  load-tested in the parity suite, but operators do not have this helper.
- `docker-bridge-smoke.yaml` is the only Docker config that loads
  successfully because it uses literal placeholder strings (`CHANGE_ME`)
  rather than `${VAR}` syntax.
- All 15 configs are in `ALL_SHIPPED_CONFIGS`
  (`test_example_configs.py:572`) and pass the hygiene tests
  (no real secrets, no deprecated language, valid storage backend, valid
  `adapter_kind`).

## Intentionally Deferred

The following are out of scope for this audit change:

- **Live/Docker hardware-tier tests.** Gated by `@pytest.mark.live` and
  `@pytest.mark.docker`; not part of the operator pre-flight workflow.
  Covered by `docs/dev/testing.md` tier table (lines 211-217).
- **Per-adapter SDK-boundary tests.** The Docker integration tests in
  `tests/integration/` and `.github/workflows/docker-integration.yml`
  validate real SDK behavior against containerized Synapse/meshtasticd.
  These are runtime tests, not config-validation tests.
- **Sample config shipping as package data.** The sample is generated by
  `generate_sample_config()` at runtime, not shipped as a static file.
  `sample.py:12-14` documents this: "Example configs for more advanced
  scenarios live in `examples/configs/` in the source repository. They are
  reference documentation, not shipped as package data."
- **`ConfigValidationError.__str__` rendering of structured attributes.**
  See F-9. The attributes are available programmatically; the CLI
  single-line rendering is appropriate for terminal operators.
- **JSON output mode for `medre config check`.** Not requested; the
  human-readable output is the operator surface. If added later, the
  structured `ConfigValidationError` attributes (F-9) should be rendered
  there.
- **Config schema JSON files under `docs/schemas/`.** These are
  machine-readable representations of the same authority the loader
  enforces. Their consistency with the loader is a schema-authority
  concern, covered by the prior `config-schema-authority-audit.md`, not
  an operator-workflow concern.
- **Cross-instance loop prevention and distributed coordination.**
  Explicitly non-guaranteed per `docs/ops/troubleshooting.md:721`. Not a
  config-validation concern.
