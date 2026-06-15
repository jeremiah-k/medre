# YAML Config Migration Audit

Factual audit of the current MEDRE configuration subsystem as a baseline for
migrating human-authored config from TOML to YAML. No aspirational language;
describes running code on branch `feat/yaml-config-migration`.

Strategic direction (context only — not normative here): YAML-only
human-authored config with a boring subset guards layer — safe loader,
duplicate-key rejection, custom-tag rejection, no anchors/aliases/merge keys,
top-level mapping only, `.yaml`/`.yml` accepted, `.toml` rejected with a clear
error. This audit enumerates the surfaces a migration touches so subsequent
implementation work can proceed in non-overlapping waves.

MEDRE is pre-release; no public API is frozen.

---

## 1. Config load entry points

The single load entry point is `load_config()` in
`src/medre/config/loader.py` (line 145). It returns a
`(RuntimeConfig, ConfigSource, MedrePaths)` triple. File discovery is
`find_config()` in the same module (line 64).

Production callers of `load_config()` (9 sites):

| Caller                              | Path                                                 |
| ----------------------------------- | ---------------------------------------------------- | ----- | -------------------------------------------- |
| `medre run`                         | `src/medre/cli/run_commands.py:94`                   |
| `medre replay`                      | `src/medre/cli/replay_commands.py:56`                |
| `medre config check`                | `src/medre/cli/config_commands.py:61`                |
| `medre config sample` (no-arg path) | `src/medre/cli/config_commands.py:236`               |
| `medre routes validate              | topology                                             | list` | `src/medre/cli/route_commands.py:16,152,253` |
| `medre diagnostics`                 | `src/medre/cli/diagnostics_commands.py:29,104`       |
| `medre smoke`                       | `src/medre/runtime/smoke.py:342`                     |
| `medre run-session`                 | `src/medre/runtime/run_session/orchestration.py:237` |
| `medre drill`                       | `src/medre/runtime/drill.py:217`                     |
| `medre evidence` bundle collector   | `src/medre/runtime/evidence/_bundle.py:107`          |

`ConfigSource` (loader.py:49) is the enum recording origin: `EXPLICIT`,
`MEDRE_CONFIG`, `MEDRE_HOME`, `XDG`, `LOCAL`.

The `--config` CLI flag is declared per-subcommand in `src/medre/cli/main.py`
(no global `--config`). The `smoke` subcommand documents its default as
`examples/configs/fake-bridge-smoke.toml` (main.py:105); the resolved default
is computed in `runtime/smoke.py` and `runtime/run_session/orchestration.py`.

---

## 2. Supported extensions & file discovery

Today the loader accepts exactly one filename: `config.toml`.

- `src/medre/config/paths.py:56` — `_CONFIG_FILENAME: str = "config.toml"`.
  This constant feeds both the XDG path (`config_dir / "config.toml"`) and the
  `MEDRE_HOME` path (`home / "config.toml"`).
- `find_config()` (loader.py:64) hardcodes three filename expectations:
  `$MEDRE_HOME/config.toml` (line 114), `$XDG_CONFIG_HOME/medre/config.toml`
  via `paths.config_file` (line 121), and `./medre.toml` (line 127).
- `load_config()` does **no extension checking**. It reads the resolved path
  as bytes (line 172) and calls `tomllib.loads` unconditionally (line 177).
  An explicit `--config foo.yaml` would reach `tomllib.loads` and fail with a
  generic `ConfigFileError: Invalid TOML`.

Search order (`find_config`, loader.py:67-137):

1. `--config` CLI flag — explicit path, must exist (`ConfigFileError` if not).
2. `MEDRE_CONFIG` env var.
3. `$MEDRE_HOME/config.toml`.
4. `$XDG_CONFIG_HOME/medre/config.toml` (default `~/.config/medre/config.toml`).
5. `./medre.toml`.

A YAML migration needs an extension gateway at `load_config` time, a new
`_CONFIG_FILENAME`/discovery set (`.yaml`/`.yml`), and a clear rejection path
for `.toml` once deprecated. The five-step search order itself is reusable.

---

## 3. TOML parser dependency & current usage

Dependency status:

- `pyproject.toml` declares exactly one runtime dependency:
  `dependencies = ["msgspec==0.21.1"]`. `requires-python = ">=3.11"`.
- No `tomli`, no third-party `toml`, no `tomli_w`. TOML parsing rides entirely
  on the Python 3.11+ stdlib `tomllib` module.
- No YAML library is currently declared. A migration adds a runtime dependency
  (PyYAML or ruamel.yaml) unless it restricts itself to a stdlib-only subset
  (there is none for YAML).

Single production parse call site:

```python
# src/medre/config/loader.py:177
data = tomllib.loads(raw.decode("utf-8"))
```

`raw` is the full file contents read as bytes (line 172). Errors are caught as
`(tomllib.TOMLDecodeError, UnicodeDecodeError)` and re-raised as
`ConfigFileError` (line 178). A non-dict result is rejected (line 181).

The migration swaps exactly one statement. Downstream consumers never see
TOML — they receive a `dict[str, Any]` (`data`) which is handed to
`_parse_runtime_config(data, paths)`. Every `from_toml_dict` factory is named
for TOML but operates on a generic dict, so a YAML-parsed dict is structurally
identical. The factories are TOML-flavored only in two ways:

- TOML produces `list` for arrays; the model coerces to `set`/`tuple` where
  annotations require it (`_coerce_adapter_kwargs` in
  `src/medre/config/model.py:44`).
- TOML keys are strings; `dict[int, str]` annotations (e.g. Meshtastic
  `channel_mapping`) are int-coerced post-parse (model.py:71).

YAML's native tag set is richer (real `set`, real `int` keys, anchors,
aliases, merge keys, custom tags). The subset-guards layer exists to keep the
post-parse dict inside the shape these coercions already expect.

---

## 4. Schema / typed validation path

Validation is layered: raw-dict checks, then frozen-dataclass construction,
then `.validate()` hooks. All of it is transport-agnostic and parser-agnostic.

### 4.1 Root model

`RuntimeConfig` (`src/medre/config/model.py:514`) is a frozen dataclass with
seven sections: `runtime`, `logging`, `storage`, `limits`, `retry`,
`adapters`, `routes`. Built by `_parse_runtime_config()` (loader.py:193).

### 4.2 Per-section validation in the loader

| Section            | Validator                                                                                              | Path          |
| ------------------ | ------------------------------------------------------------------------------------------------------ | ------------- |
| `[logging]`        | `_validate_logging_section()` — level/format/overrides types and permitted values, before construction | loader.py:347 |
| `[retry]`          | `_validate_retry_section()` — int/float/bool type and range                                            | loader.py:288 |
| `[runtime.limits]` | `RuntimeLimits.validate()` — non-positive guards + upper-bound warnings                                | model.py:221  |
| `[storage]`        | placeholder expansion via `paths.expand_placeholder`                                                   | loader.py:238 |

### 4.3 Adapter wrappers

Each transport has a runtime wrapper (`MatrixRuntimeConfig`,
`MeshtasticRuntimeConfig`, `MeshCoreRuntimeConfig`, `LxmfRuntimeConfig`) with a
`from_toml_dict(instance_name, data)` classmethod (model.py:280, 324, 359,
394). Shared flow: pop wrapper fields (`enabled`, `adapter_id`,
`adapter_kind`), reject bad `adapter_kind`, coerce remaining kwargs via
`_coerce_adapter_kwargs`, construct and `.validate()` the transport config.
`AdapterConfigSet.validate()` (model.py:470) rejects duplicate `adapter_id`
values across all transports.

Transport config dataclasses live in `src/medre/config/adapters/` (`matrix.py`,
`meshtastic.py`, `meshcore.py`, `lxmf.py`), each with its own `.validate()`.

### 4.4 Routes

`RouteConfigSet.from_toml_dict(data)` (`src/medre/config/routes.py:846`)
iterates the top-level `routes` table and constructs `RouteConfig` per entry.
`RouteConfig.from_toml_dict` (routes.py:433) performs extensive structural
validation — see §9 for the shapes that matter for YAML. `RouteConfigSet.
validate()` (routes.py:822) rejects duplicate route IDs.

### 4.5 Error hierarchy

`src/medre/config/errors.py`: `ConfigError` base → `ConfigNotFoundError`,
`ConfigValidationError` (carries `transport`, `adapter_id`, `section_path`),
`ConfigFileError`. All `from_toml_dict` factories raise
`ConfigValidationError` with a dot-separated `section_path`. This error
vocabulary is reusable verbatim for YAML — only the parse step changes.

---

## 5. Environment-variable & secret resolution

Implemented in `src/medre/config/env.py`. Env vars are applied **on top of**
the file-loaded `RuntimeConfig`; the original is never mutated
(`dataclasses.replace`).

### 5.1 Patterns

| Pattern                                                                             | Module symbol                                    | Resolves to                                                                       |
| ----------------------------------------------------------------------------------- | ------------------------------------------------ | --------------------------------------------------------------------------------- |
| `MEDRE_HOME`, `MEDRE_CONFIG`, `MEDRE_DB_PATH`, `MEDRE_LOG_LEVEL`, `MEDRE_RUNTIME_*` | `CORE_ENV_NAMES`, `_ENV_FIELD_MAP`               | core fields                                                                       |
| `MEDRE_ADAPTER__<TOKEN>__<FIELD>`                                                   | `_ADAPTER_ENV_PREFIX`, `_parse_adapter_env_vars` | per-instance adapter overrides; also creates new adapters when `TRANSPORT` is set |
| `MEDRE_ROUTE__<TOKEN>__<FIELD>`                                                     | `ROUTE_ENV_PREFIX`, `_parse_route_env_vars`      | per-instance route overrides; also creates new routes                             |
| `MEDRE_RETRY__<FIELD>`                                                              | `RETRY_ENV_PREFIX`, `_parse_retry_env_vars`      | `[retry]` overrides                                                               |

Token normalization: `normalize_adapter_id()` (env.py:338) — uppercases,
replaces non-alphanumerics with `_`, collapses repeats, strips edges.
`detect_token_collisions()` (env.py:358) rejects two adapter IDs that
normalize to the same token.

### 5.2 Legacy rejection

`_REJECTED_LEGACY_PREFIXES` (env.py:116): `MEDRE_MATRIX_`, `MEDRE_MESHTASTIC_`,
`MEDRE_MESHCORE_`, `MEDRE_LXMF_`. Presence raises `ConfigValidationError`
with migration guidance.

### 5.3 Secret handling

Heuristic detection: `_SECRET_FIELD_RE` (env.py:201) matches
`TOKEN|SECRET|PASSWORD|KEY|AUTH|CREDENTIAL|BLE|IDENTITY` (case-insensitive).
`EnvProvenance.redacted_items()` (env.py:263) emits `***REDACTED***` for
matching field segments. Raw values still flow through to the config.

Transport-level hardening (independent of file format):

- `MatrixConfig.__repr__` redacts tokens to a short `syt_…` preview.
- MeshCore `node_config` rejects keys named `private_key`, `secret`,
  `password` at validation time.

None of the env-var machinery touches the parser. It operates on the
post-load `RuntimeConfig` and is fully reusable under YAML.

---

## 6. Example validation path

Two test files own the "examples load" contract:

- `tests/test_example_configs.py` — iterates `examples/configs/*.toml`, calls
  `tomllib.loads` for structural assertions and `load_config()` for full
  load+validate. The `_ALL_CONFIG_FILES` glob (line 972) auto-covers any new
  file added to the directory. Per-file `TestCase` subclasses hold deep
  per-field assertions for `fake-bridge-smoke.toml`, `fake-multi-adapter.toml`,
  `docker-matrix-bridge.toml`, `docker-meshtastic-bridge.toml`,
  `live-matrix-meshtastic.toml`, etc.
- `tests/test_config_runtime_parity.py` — parametrized load+build parity over
  `fake-bridge-smoke.toml`, `fake-multi-adapter.toml`,
  `docker-matrix-bridge.toml`, `docker-meshtastic-bridge.toml` (lines 372-377).

Structural guards in `tests/test_docs_misc_consistency.py`:

- `test_no_tcp_port_in_example_toml` (line 348) — globs
  `examples/configs/*.toml` and rejects the legacy `tcp_port` key.
- `test_live_config_helper_uses_port_not_tcp_port` (line 376) — pins
  `tests/helpers/live_config.py::write_live_bridge_toml` to emit `port =`.

`tests/helpers/live_config.py::write_live_bridge_toml` builds live-bridge
config text via f-string template (comment at line 306 notes it deliberately
avoids `tomli_w`).

A YAML migration touches all of these: the glob extension filter, the
in-test `tomllib.loads` calls, and the live-config helper's f-string emitter.

---

## 7. TOML-dependent tests

### 7.1 Tests that import `tomllib` directly

| File                                           | Use                                                         |
| ---------------------------------------------- | ----------------------------------------------------------- |
| `tests/test_example_configs.py`                | parse example `.toml` for structural assertions             |
| `tests/test_config_loader.py`                  | loader round-trips; `tomllib.loads` on sample output        |
| `tests/test_cli_config_commands.py`            | sample-command output round-trip                            |
| `tests/test_cli_config_and_smoke.py`           | sample round-trip, walkthrough fixtures                     |
| `tests/test_cli_config_workflows.py`           | sample round-trip, duplicate-key check                      |
| `tests/test_cli_run_commands.py`               | sample round-trip                                           |
| `tests/test_clean_environment.py`              | `pyproject.toml` parse; sample round-trip; SDK-check output |
| `tests/test_packaging_and_install_contract.py` | `pyproject.toml` metadata contract                          |
| `tests/test_environment_reproducibility.py`    | `pyproject.toml` parse for reproducibility                  |
| `tests/test_cli_install_metadata.py`           | `pyproject.toml` entry-point/classifier checks              |
| `tests/test_matrix_auth_login.py`              | token sidecar write/read round-trip via `.toml`             |
| `tests/test_matrix_auth_live.py`               | live bridge config parse (live tier)                        |
| `tests/test_live_matrix_meshtastic_bridge.py`  | parse `live-matrix-meshtastic.toml` (live tier)             |
| `tests/test_operator_recovery.py`              | parse CLI stderr/stdout as TOML for diagnostics checks      |

Note: `pyproject.toml` reads (packaging, environment, install, scope) are
**not** part of the runtime config migration — `pyproject.toml` stays TOML.
Only the config-subsystem reads need to move.

### 7.2 Tests that write `.toml` fixtures (selected)

`tests/conftest.py` (lines 319, 326, 333), `tests/test_config_loader.py`,
`tests/test_cli_run_workflows.py`, `tests/test_routes.py`,
`tests/test_routes_channel_room_map.py`, `tests/test_evidence_cli.py`,
`tests/test_trace.py`, `tests/test_cli_inspect_commands.py`,
`tests/test_route_retry_command_surface.py`, `tests/test_operator_failures.py`,
`tests/test_runtime_operator_recovery_v2.py`,
`tests/test_meshtastic_env_first.py`, `tests/test_meshtastic_outbound_gate.py`,
`tests/test_shutdown_under_traffic.py`, and others — these write TOML strings
to `tmp_path` and pass them to `load_config`. A YAML migration either keeps a
TOML path for legacy tests or migrates the fixtures in lockstep.

### 7.3 The shipped sample config

`src/medre/config/sample.py::generate_sample_config()` returns a TOML string
consumed by `medre config sample` and round-tripped through `tomllib.loads` in
several tests above. The CLI text at `src/medre/cli/main.py:105` and the
sample header (sample.py:24-25) both say "TOML".

---

## 8. Docs & runbooks mentioning `.toml`

Documents that reference TOML config files or the `.toml` extension (these
will need paired updates when YAML lands):

| Document                                                 | Notable `.toml` references                                                                                             |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `docs/spec/configuration.md`                             | normative TOML schema block (§3), search order (§4)                                                                    |
| `docs/ops/configuration.md`                              | full TOML reference, path placeholders, env overrides, secrets                                                         |
| `docs/ops/install.md`                                    | `medre config sample > /tmp/medre-alpha.toml`, example table                                                           |
| `docs/ops/running-medre.md`                              | `medre run --config config.toml`, MEDRE_HOME layout tree                                                               |
| `docs/ops/troubleshooting.md`                            | `/tmp/bad-syntax.toml`, `/tmp/bad-route.toml`, `/tmp/dup-route.toml`, `/tmp/missing-sdk.toml`, `/tmp/bad-storage.toml` |
| `docs/ops/live-validation/matrix-meshtastic-meshcore.md` | `medre.toml`, `medre-3way.toml`                                                                                        |
| `docs/ops/operator-workflows.md`                         | example config references                                                                                              |
| `examples/configs/README.md`                             | table of all example `.toml` files, quick-start `--config` commands                                                    |
| `docs/changes/unreleased.md`                             | change-fragment references                                                                                             |

Per `docs/dev/README.md`, the old runbooks tree was replaced; current
runbook-style content lives under `docs/ops/`. The example README's pointer to
the former secure-credentials and configuration runbook pages is
stale and tracked separately — it is not blocking for this migration.

Spec authority rule (from `docs/spec/README.md`): if `docs/spec/` conflicts
with other docs, `spec/` wins. A YAML migration therefore has to update
`docs/spec/configuration.md` in the same change, with the ops pages following
the spec. RFC 2119 keywords belong only in `docs/spec/`.

---

## 9. Route shapes that should become structured YAML

Routes carry the most nested structure in the config. These are the shapes
YAML expresses natively as nested mappings/sequences and that benefit most
from the migration. All validation lives in `src/medre/config/routes.py`.

### 9.1 `channel_room_map`

`RouteConfig.channel_room_map: dict[str, str] | None` (routes.py:429). Maps
Meshtastic channel indices ("0"–"7") to canonical Matrix room IDs. Validation
(routes.py:598-721):

- Must be a table/dict; mutually exclusive with `source_channel`,
  `dest_channel`, `source_room`, `dest_room`.
- Requires exactly one source and one dest adapter.
- Integer-typed keys are coerced to canonical `"0"`…`"7"` strings; out-of-range
  and duplicates rejected.
- Values must be canonical room IDs (`!` prefix); aliases (`#` prefix)
  rejected. Empty map rejected.

Example today (`examples/configs/live-matrix-meshtastic-channel-map.toml:79`):

```toml
channel_room_map = { 0 = "!general:example.com", 1 = "!admin:example.com", 2 = "!alerts:example.com" }
```

YAML inline mapping reads more naturally and avoids TOML's mixed bare-key
syntax. Note: YAML flow `{0: "!general:..."}` and block mapping both work;
block form is preferred for readability.

### 9.2 `source_origin_label` / `dest_origin_label`

`RouteConfig.source_origin_label` and `dest_origin_label`
(routes.py:430-431, parsed at 562-595). String-or-None, type-checked (booleans
rejected explicitly to avoid `True`/`False` coercion bugs). These are
rendering-context metadata, not routing keys — see
`docs/dev/relay-prefix-attribution-audit.md` for the consumption side. Simple
scalars; no structural YAML advantage, but they belong with the route table.

### 9.3 Route policy (`[routes.<id>.policy]`)

`BridgePolicy` (routes.py:78), constructed via `BridgePolicy.from_toml_dict`
(routes.py:131). Six allowlist fields, all `tuple[str, ...]`:

- `allowed_event_types`, `allowed_source_adapters`, `allowed_dest_adapters`,
  `room_allowlist`, `channel_allowlist`, `sender_allowlist`.

Validation rejects unknown keys, bare strings (silently becomes a tuple of
characters in Python), non-list/tuple values, and non-string elements.
`_KNOWN_FIELDS` (routes.py:110) is the allowlist of accepted keys.

Under YAML these are native sequence values. The "bare string becomes a tuple
of characters" guard is TOML/Python-specific; YAML has the same Python
post-parse risk (a YAML string is still a Python str), so the guard transfers
directly.

### 9.4 Route retry (`[routes.<id>.retry]`)

`RouteRetryConfig` (routes.py:237), via `from_toml_dict` (routes.py:268).
Five fields: `enabled`, `max_attempts`, `backoff_base`, `max_delay_seconds`,
`jitter`. Type and range validation. YAML scalar block; no structural
advantage but groups naturally under the route.

### 9.5 Adjacent transport-level structured fields

Not route fields, but part of the same migration surface and consumed by the
same `_coerce_adapter_kwargs` coercion layer:

- Meshtastic `channel_mapping: dict[int, str]` — TOML string keys are
  int-coerced post-parse (model.py:71). YAML can express true integer keys,
  but the coercion layer should stay defensive.
- MeshCore `node_config: dict` — opaque node settings; rejects secret-named
  keys at validation time.
- Matrix `room_allowlist: set[str]`, `auto_join_rooms: tuple[str, ...]` —
  TOML lists coerced to set/tuple (model.py:63-68).

### 9.6 Departed keys (do not resurrect)

`RouteConfig.from_toml_dict` rejects `filter_hooks` (routes.py:516-521) —
reserved and unsupported. The old `[[routes]]` array-of-tables shape with
`from_adapter`/`to_adapter`/`from_channel`/`to_channel`/`event_kinds`/
`direction` shown in `docs/spec/configuration.md` §3 (lines 148-158) is a
historical illustration; the running code uses `[routes.<id>]` tables with
`source_adapters`/`dest_adapters`/`directionality`. The YAML schema should
match the running `[routes.<id>]` shape, not the departed array form.

---

## 10. Migration considerations

Sequencing notes for the non-overlapping implementation waves this audit
unblocks. Each wave is independently testable.

1. **Parser swap + extension gateway.** Add YAML loader alongside `tomllib`;
   gate on file extension in `load_config`; add `.toml`-rejects-with-clear-error
   path. Touches only `src/medre/config/loader.py` and `paths.py`. Reusable
   validation pipeline (§4) is untouched.
2. **Subset guards.** Safe loader, duplicate-key rejection, custom-tag
   rejection, no anchors/aliases/merge keys, top-level mapping only. Sits
   between the parser and `_parse_runtime_config`. Emits `ConfigFileError`/
   `ConfigValidationError` with the existing error vocabulary.
3. **Example + sample migration.** Convert `examples/configs/*.toml` and
   `src/medre/config/sample.py::generate_sample_config()`; update
   `tests/test_example_configs.py` and the round-trip tests in §7.1.
4. **Fixture migration.** Update the `.toml`-writing test fixtures in §7.2
   (or keep a legacy TOML path during transition).
5. **Docs sweep.** Update `docs/spec/configuration.md` first (spec authority),
   then the `docs/ops/` pages and `examples/configs/README.md` listed in §8.

Waves 1-2 are parser-only and do not change `RuntimeConfig` shape. Waves 3-5
are content-only and do not change parsing. The `from_toml_dict` factories
remain valid post-migration because they consume a generic dict; only their
docstrings/names are TOML-flavored.

Out of scope for this audit (separate decisions):

- Whether `MEDRE_HOME` discovery looks for `config.yaml` vs `config.yml` vs
  both.
- Whether `MEDRE_CONFIG` keeps accepting `.toml` during a transition window.
- Whether to rename `from_toml_dict` factories (cosmetic; large diff).
- Whether to add a JSON Schema for YAML config under `docs/schemas/`.

---

## 11. Files inspected

### 11.1 Source — config subsystem

- `src/medre/config/__init__.py`
- `src/medre/config/loader.py`
- `src/medre/config/paths.py`
- `src/medre/config/model.py`
- `src/medre/config/routes.py`
- `src/medre/config/env.py`
- `src/medre/config/errors.py`
- `src/medre/config/sample.py`
- `src/medre/config/adapters/__init__.py`
- `src/medre/config/adapters/errors.py`
- `src/medre/config/adapters/matrix.py` (via grep)
- `src/medre/config/adapters/matrix_credentials.py` (via glob)
- `src/medre/config/adapters/meshtastic.py` (via grep)
- `src/medre/config/adapters/meshcore.py` (via glob)
- `src/medre/config/adapters/lxmf.py` (via glob)

### 11.2 Source — callers and CLI

- `src/medre/cli/main.py`
- `src/medre/cli/run_commands.py` (via grep)
- `src/medre/cli/replay_commands.py` (via grep)
- `src/medre/cli/config_commands.py` (via grep)
- `src/medre/cli/route_commands.py` (via grep)
- `src/medre/cli/diagnostics_commands.py` (via grep)
- `src/medre/runtime/smoke.py` (via grep)
- `src/medre/runtime/drill.py` (via grep)
- `src/medre/runtime/run_session/orchestration.py` (via grep)
- `src/medre/runtime/evidence/_bundle.py` (via grep)
- `src/medre/runtime/docker_bridge_artifacts.py` (via grep)

### 11.3 Examples

- `examples/configs/README.md`
- `examples/configs/live-matrix-meshtastic-channel-map.toml`
- `examples/configs/` directory listing (15 `.toml` files + README)

### 11.4 Docs

- `docs/dev/TESTING_GUIDE.md`
- `docs/dev/testing.md`
- `docs/dev/README.md`
- `docs/dev/relay-prefix-attribution-audit.md` (style reference)
- `docs/spec/README.md`
- `docs/spec/configuration.md`
- `docs/ops/README.md`
- `docs/ops/configuration.md`
- `docs/ops/troubleshooting.md` (via grep)
- `docs/ops/running-medre.md` (via grep)
- `docs/ops/install.md` (via grep)
- `docs/ops/live-validation/matrix-meshtastic-meshcore.md` (via grep)

### 11.5 Tests

- `tests/test_example_configs.py` (via grep)
- `tests/test_config_loader.py` (via grep)
- `tests/test_config_runtime_parity.py` (via grep)
- `tests/test_docs_misc_consistency.py` (via read + grep)
- `tests/helpers/live_config.py` (via grep)
- `tests/helpers/walkthrough.py` (via grep)
- Plus the `tomllib`-importing and `.toml`-writing test files enumerated in §7.

### 11.6 Repo metadata

- `pyproject.toml`
- `AGENTS.md`
- `README.md`

### 11.7 Testing-guide check (per task requirement)

The task required locating and reading any testing guide before editing. Two
testing guides exist and were read in full:

- `docs/dev/testing.md` — the authoritative testing guide (770 lines). Covers
  test style, file-size limits, adapter tiers, storage tests, patch target
  policy, docker/live tiers, agent verification sequence, partition strategy.
- `docs/dev/TESTING_GUIDE.md` — the Resource-Warning Testing Guide (589
  lines). Covers `ResourceWarning`/`PytestUnraisableExceptionWarning`
  prevention, sqlite lifecycle, asyncio loop rules, AsyncMock decision table.

No separate runbook testing material exists; `docs/dev/README.md`
states the runbooks tree was replaced and current content lives under
`docs/ops/` and `docs/dev/`.

### 11.8 Markdown validation

No markdown linter is configured in the repository (no `markdownlint` config,
no prettier, no committed formatter — confirmed via `AGENTS.md` coding-style
note). Validation was therefore not run; the document follows the ATX
heading style, 88-column prose wrap, and table conventions used by the
neighboring audit files in `docs/dev/`.
