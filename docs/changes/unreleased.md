# Unreleased Changes

Pre-release MEDRE. All changes below are unreleased and subject to change
without notice. Append new entries to the bottom of this file — do **not**
create per-commit fragment files.

---

## Transport Capability Semantics and Delivery Evidence

Document implemented transport capability semantics, rendering budget
behavior, suppression/truncation evidence, relation/reaction degradation,
replay parity expectations, and unknown capability behavior.

**Changed:**

- `docs/spec/adapter-runtime.md`: CapabilityLevel decision mapping,
  evidence signal descriptions.
- `docs/spec/routing-delivery.md`: unknown event kind passthrough,
  fail-closed for unknown relation types, dormant fallback gap.
- `docs/spec/diagnostics-evidence.md`: capability-evidence derivation,
  rendering budget enforcement and evidence.
- `docs/spec/conformance.md`: transport capability conformance table.
- `docs/spec/appendices/transport-limitations.md`: capability semantics
  known gaps.
- `docs/ops/recovery-and-replay.md`: capability filtering during replay.
- `docs/ops/troubleshooting.md`: capability suppressed diagnosis.
- `docs/ops/operator-workflows.md`: capability_suppressed failure kind.

---

## Queued Delivery Outbox Correlation and Terminal Outcome Reporting

Add exact `outbox_id`/`attempt_number` correlation for async queued
adapters, stale callback protection, terminal queue outcome reporting,
and remove `delivery_plan_id=None` legacy fallback.

---

## Retry Route-Decision Parity

Persist route-decision metadata in outbox item metadata at creation time
and recover it during retry reconstruction so retry delivery matches the
original live delivery decision.

---

## OutboxManager Extraction

Extract outbox lifecycle operations from `PipelineRunner` into a dedicated
`OutboxManager` module. Pure refactoring — no behavior changes.

---

## Meshtastic Configurable Packet Routing

Add configurable packet classification policy to the Meshtastic adapter.

---

## Adapter Ingress Evidence Parity

Harden post-stop ingress behavior and fill LXMF diagnostics evidence gaps.

---

## MeshCore Per-Contact Retry Timeout Cache Clear

Clear MeshCore per-contact retry timeout cache on reconnect and
failed-start cleanup.

---

## Matrix Adapter start() Lifecycle Cleanup

Roll back Matrix adapter lifecycle fields on failed start; move started
log after completion.

---

## Adapter Startup Lifecycle Cleanup

Harden start-failure cleanup across MeshCore, LXMF, and Meshtastic
adapters to match the Matrix pattern.

---

## MeshCore BLE Reconnect Fix

Fix BLE connection failures on Linux BlueZ stacks where
le-connection-abort-by-local errors abort the initial connect, and
stale BlueZ state prevents reconnect.

---

## Relay Attribution Prefix — Transport Profile Documentation

Document cross-transport relay attribution prefix model, config fields,
and truncation semantics for all four transports.

**New config fields:**

- `meshcore_relay_prefix` (string, default `""`)
- `lxmf_relay_prefix` (string, default `""`)

---

## LXMF Announce Interval Configuration

Add configurable periodic LXMF announce interval for mesh path discovery.

**New config field:** `announce_interval_seconds` (float, default `600.0`).

---

## origin_label — Platform-Neutral Source Label

Added platform-neutral `origin_label` to all adapter configs. Matrix
prefix is now target-local via `MatrixConfig.relay_prefix`. LXMF renderer
is target-aware.

**New config fields:**

- `origin_label` (string, default `""`) on all four adapter configs.
- `relay_prefix` (string, default `""`) on `MatrixConfig`.

---

## Remove meshnet_name and matrix_relay_prefix from MeshtasticConfig

Removed `matrix_relay_prefix` from `MeshtasticConfig`. Removed
`meshnet_name` from all transport profile config tables and prefix
template variable tables. `{origin_label}` is the single MEDRE-generic
source label.

**Breaking:** existing configs with `meshnet_name` or `matrix_relay_prefix`
will not load. Rename `meshnet_name` to `origin_label` and move
`matrix_relay_prefix` to `MatrixConfig.relay_prefix`.

---

## Clean Attribution Surface — Canonical Variables Only

Finalized the attribution surface to use only canonical template
variables. Old variables (`{longname}`, `{shortname}`, `{shortname5}`,
`{from_id}`, `{meshnet_name}`) are unknown placeholders.

**Canonical variables:** `{sender}`, `{sender_short}`, `{sender_id}`,
`{sender_handle}`, `{platform}`, `{route_id}`, `{channel}`,
`{origin_label}`.

mmrelay `KEY_MESHNET` is an isolated wire-compatibility field, not a
MEDRE attribution variable.

---

## Direction-aware Route Origin Labels

Replace the single `origin_label` route field with direction-aware
`source_origin_label` and `dest_origin_label`.

- `source_origin_label`: applied to forward legs (source→dest).
- `dest_origin_label`: applied to reverse legs (dest→source).
- Both default to `None` (fall back to adapter `origin_label`).

Per-channel origin labels are not implemented. Use separate routes per
channel.

---

## Adapter Projection / Core Boundary Documentation

Document the structural boundary between core rendering (generic
`RelayAttribution`) and adapter-adjacent native projection. Document the
`origin_label` precedence chain.

---

## Dispatch Refactor, platform_hint, Explicit Empty Labels

Refactored attribution dispatch to be truly dispatch-only: detects
platform and delegates to per-adapter projection helpers with no
cross-platform identity enrichment. Wired `platform_hint` from
`SourceAttributionConfig`. Preserved explicit empty origin labels
(`""` = suppress, `None` = unset). Cleaned MatrixRenderer registration
to be Matrix-config-driven.

- `_attribution_dispatch.py`: detects platform, delegates to adapter
  projection helpers, returns projected fields. No global flat-key
  fallback — each adapter handles its own native keys.
- `project_source_fields` / `detect_source_platform`: accept `platform_hint`.
- All renderers: `is not None` checks for `ctx.source_origin_label`.
- `derive_meshnet_value`: `is not None` checks.
- MatrixRenderer: registers when Matrix configs exist (not Meshtastic).
- Underscore-prefixed adapter modules treated as shared infrastructure.

---

## Transport-Native Identity Enrichment

Constrain sender-identity enrichment to adapter-local projection so live
bridge prefixes are more readable. Each adapter projects its native
sender identity into the generic `RelayAttribution` sender fields;
core rendering stays transport-neutral.

**Per-transport projection:**

- Matrix: MXID → `source_sender_id` and `source_sender_handle`;
  display name → `source_sender_label` (display-name only, no localpart
  fallback); MXID localpart → `source_sender_short_label`. Display name
  is never converted to mmrelay `KEY_LONGNAME`/`KEY_SHORTNAME`.
- Meshtastic: `from_id` → `source_sender_id`; node-database
  `longname`/`shortname` (read in-memory at ingress) → label fields with
  a deterministic fallback chain.
- MeshCore: pubkey prefix → `source_sender_id`; local contact
  `adv_name` → `source_sender_label` when the sender is a known contact;
  opaque pubkey never becomes a label.
- LXMF: `source_hash` → `source_sender_id`; captured display name → label
  fields; opaque hash never becomes a label.

**Opacity rule:** opaque identifiers (LXMF hash, MeshCore pubkey prefix)
never populate `source_sender_label`; `{sender}` renders empty rather
than a truncated hash or pubkey. Operators use `{sender_id}` for the
opaque value.

**Observational only:** identity enrichment is not delivery evidence, not
authoritative storage state, and may be stale. Prefix rendering remains
safe when all identity labels are empty. No canonical topology or contact
events are emitted.

Announce-based LXMF display-name enrichment is now implemented
(see below). Per-channel origin labels remain unsupported — operators
use separate routes per channel.

---

## Namespace Meshtastic Identity Metadata

Namespace Meshtastic identity keys under `meshtastic.*` so
transport-specific metadata stays namespaced by transport.

**Changed:**

- `meshtastic.from_id`, `meshtastic.longname`, and
  `meshtastic.shortname` are now the emitted identity keys.
- Bare `longname`/`shortname` removed from codec output; projection and
  renderer read bare `longname`/`shortname` only as legacy input
  tolerance for stored events and test fixtures produced before
  namespacing.
- Bare `from_id` retained for non-identity consumers (`source_native_ref`,
  relation mapping); non-identity keys (`packet_id`, `channel`, `to_id`,
  `reply_id`, `emoji`) remain bare.
- Platform detection tightened: namespaced `meshtastic.*` keys are the
  primary detection signal; `channel` is excluded from the legacy
  bare-key set so a sparse dict carrying only `channel` no longer
  triggers a false Meshtastic detection.
- `MatrixRenderer._resolve_mmrelay_sender_names` reads
  `meshtastic.longname`/`meshtastic.shortname` (primary) before mmrelay
  wire fields and legacy bare keys.

mmrelay wire fields (`meshtastic_longname`, `meshtastic_shortname`,
`meshtastic_meshnet`) remain separate external wire-format fields and are
not MEDRE native metadata.

---

## Extended Meshtastic Metadata Namespacing and Generic Relation Planning

Extend `meshtastic.*` namespacing to cover non-identity packet metadata
alongside identity keys. Remove direct native-identity reads from core
relation enrichment; replace with generic sender-projection callback wired
by the runtime.

**Changed:**

- Meshtastic codec now emits namespaced forms for all packet metadata
  (`meshtastic.packet_id`, `meshtastic.channel`, `meshtastic.portnum`,
  `meshtastic.to_id`, `meshtastic.is_direct_message`,
  `meshtastic.reply_id`, `meshtastic.emoji`, `meshtastic.emoji_flag`)
  alongside the retained bare forms. Bare forms remain for non-identity
  consumers and legacy stored-event tolerance; the namespaced form is
  primary for new readers.
- `_MESHTASTIC_NAMESPACED_KEYS` detection set in
  `_attribution_dispatch.py` expanded to include the new non-identity
  namespaced keys.
- `MatrixRenderer._resolve_mmrelay_packet_id` reads
  `meshtastic.packet_id` (primary) with bare `packet_id` fallback for
  legacy stored events and test fixtures.
- Core relation enrichment (`RelationEnricher`) no longer reads
  transport-native identity keys (`displayname`,
  `meshtastic.longname`, bare `longname`, bare `sender`). Sender labels
  for `original_sender_displayname` and `original_sender` are sourced
  exclusively from a generic `SenderProjectionFn` callback wired by the
  runtime builder. When no callback is wired, `original_sender` falls
  back only to the generic `source_transport_id` field (adapter-neutral,
  not an identity key); `original_sender_displayname` stays unset.
- `PipelineConfig.project_sender_metadata_fn` and runtime builder
  `_build_project_sender_metadata_fn` wire the adapter-local attribution
  dispatch into core planning, preserving layering.

**Docs updated:**

- `docs/spec/routing-delivery.md`: core-planning generic-projection clause.
- `docs/spec/transport-profiles/meshtastic.md`: non-identity namespaced
  keys, platform detection.
- `docs/spec/transport-profiles/matrix.md`: mmrelay KEY_ID resolution.
- `docs/dev/transport-native-metadata-namespacing-audit.md`: Meshtastic
  section, consumer mapping, migration status.
- `docs/dev/relay-prefix-attribution-audit.md`: Matrix envelope KEY_ID
  resolution, projection architecture for relation enrichment.
- `docs/dev/transport-native-identity-enrichment-audit.md`: core-planning
  callback note.

---

## LXMF Announce-Based Display-Name Enrichment

Add announce-cache display-name resolution to the LXMF adapter so
`{sender}` populates for LXMF-origin events when the sender is a
locally-known Reticulum identity.

**Changed:**

- `src/medre/adapters/lxmf/session.py`: new
  `resolve_display_name(source_hash)` method — synchronous local
  announce-cache lookup via `RNS.Identity.recall_app_data` +
  `LXMF.display_name_from_app_data`. No network call. Never raises.
- `src/medre/adapters/lxmf/adapter.py`: new
  `_resolve_display_name` and `_enrich_with_display_name` ingress
  wiring in `_on_packet` and `simulate_inbound`. Enrichment runs before
  codec decode and only fills in when the packet lacks a display name.

**Precedence:** message-carried `source_name` > announce-cache resolved
display name > `None`. The opaque `source_hash` is never promoted to
`{sender}`.

**Added:**

- `tests/test_lxmf_session_display_name.py`: focused tests for
  `resolve_display_name`.
- `tests/test_lxmf_adapter_display_name.py`: focused tests for adapter
  ingress enrichment.
- `tests/test_lxmf_identity_enrichment.py`: integration tests for the
  full enrichment pipeline.

**Updated:**

- `docs/dev/transport-native-identity-enrichment-audit.md`: LXMF section
  — announce enrichment implemented; new enrichment pipeline subsection.
- `docs/spec/transport-profiles/lxmf.md`: Display-Name Capture and
  Announce-Based Enrichment sections updated with normative statements.

**Unchanged (by design):**

- `src/medre/adapters/lxmf/attribution.py`: already projects
  `lxmf.display_name` → `source_sender_label` with strict typing.
- `src/medre/adapters/lxmf/codec.py`: already maps `source_name` →
  `lxmf.display_name`.
- Core rendering, relation enrichment, routing, delivery, storage,
  evidence: no changes.

---

## Example Configs and Documentation Moved from TOML to YAML

Convert all shipped example configs and primary user-facing documentation
from TOML to YAML. This is a documentation and example change only; it does
not alter parser/runtime/test behavior (the parser swap is owned by a
separate wave). `pyproject.toml` stays TOML — it is packaging metadata, not
runtime configuration.

**Changed:**

- `examples/configs/`: replaced all 15 `*.toml` files with equivalent
  `*.yaml` files. Base names preserved (including
  `mixed-matrix-meshtastic.yaml`, retained as a superseded historical
  reference). All 15 converted configs were validated to parse as a boring
  YAML subset (explicit mappings/lists only) and to be semantically
  identical to their TOML originals.
- `examples/configs/README.md`: now references `.yaml` files and documents
  the boring-YAML subset and quoting rules.
- `docs/spec/configuration.md`: normative schema block, search order, and
  prose now use YAML. Route schema updated to the running
  `routes.<id>` shape (`source_adapters`/`dest_adapters`/`directionality`)
  rather than the historical `[[routes]]` array form.
- `docs/spec/index.md`, `docs/spec/routing-delivery.md`: TOML mentions
  updated to YAML.
- `docs/ops/configuration.md`: full TOML reference rewritten as YAML.
- `docs/ops/README.md`, `docs/ops/install.md`, `docs/ops/running-medre.md`,
  `docs/ops/troubleshooting.md`, `docs/ops/operator-workflows.md`,
  `docs/ops/recovery-and-replay.md`, `docs/ops/diagnostics-and-evidence.md`,
  `docs/ops/live-validation/matrix-meshtastic.md`,
  `docs/ops/live-validation/matrix-meshtastic-meshcore.md`,
  `docs/ops/transport-setup/matrix.md`,
  `docs/ops/transport-setup/meshtastic.md`: user-facing `.toml` filenames,
  `medre.toml`/`config.toml` references, and TOML code blocks converted to
  `.yaml`/`medre.yaml`/`config.yaml` and YAML blocks.
- `docs/dev/operator-surface-audit.md`, `docs/dev/release-readiness-audit.md`,
  `docs/dev/source-audits.md`, `docs/dev/resource-lifecycle.md`:
  TOML-format mentions in operator/runtime descriptions updated to YAML.

**YAML subset and quoting rules applied:**

- Explicit mappings and sequences only. No anchors, aliases, merge keys, or
  custom tags.
- Quoted values that YAML could misread: Matrix room IDs (`"!room:server"`),
  MXIDs (`"@user:server"`), channel IDs where string semantics matter
  (`"0"`), BLE/MAC addresses, `${ENV}` placeholders, and path placeholders
  like `"{state}/medre.sqlite"`.
- `channel_room_map` and `channel_mapping` keys are quoted strings
  (`"0"`, `"1"`, ...) to preserve channel-index string semantics; the
  loader coerces these to canonical `"0"`–`"7"` keys.

**Intentionally untouched (out of scope for this wave):**

- Parser/loader/runtime/sample-config source (`src/medre/config/**`,
  `src/medre/cli/**`), tests, and JSON Schemas — owned by separate
  implementation waves.
- `pyproject.toml` references in docs (packaging metadata, stays TOML).
- `docs/spec/security-privacy.md`, `docs/spec/conformance.md`,
  `docs/spec/storage.md`, `docs/dev/yaml-config-migration-audit.md`, and
  `docs/dev/live-test-harness.md` — TOML mentions there are out of scope for
  this docs/examples wave and remain for follow-up.

**Breaking:** existing `medre.toml`/`config.toml` files must be renamed to
`.yaml` (or `.yml`). The loader accepts `.yaml`/`.yml` and rejects `.toml`
with a clear error.

---

## Wrap Invalid-UTF-8 Config Files as ConfigFileError

`load_config` decoded the config file with
`path.read_text(encoding="utf-8")` inside a `try` that only caught
`OSError`. A `UnicodeDecodeError` (a `ValueError`, not an `OSError`)
from a non-UTF-8 config escaped unwrapped, surfacing as a raw codec
traceback to the operator. The `except UnicodeDecodeError` handler
existed but guarded `parse_yaml_config`, which receives an
already-decoded `str` and could never raise it.

**Fixed:** the `UnicodeDecodeError` handler now guards the `read_text`
call, so a non-UTF-8 config file raises
`ConfigFileError("Config file <path> is not valid UTF-8: ...")` —
consistent with the other file-read error paths.

---

## Reject Exotic Mapping Key Types in Strict YAML Loader

The strict YAML loader now raises `StrictYAMLError` for mapping keys
whose type is not one of the plain scalar types (`str`, `int`, `float`,
`bool`, `None`). Previously, exotic hashable keys produced by tags like
`!!omap` (tuples) or `!!set` (frozensets) were silently accepted. The
check is enforced in both the loader constructor (`construct_mapping`)
and the post-parse type walk (`_validate_plain_types`).

Configs that relied on exotic mapping keys will now fail at load time
with `"unsupported mapping key type <type>; only plain scalar keys are
allowed"` instead of passing through and potentially causing downstream
misconfiguration.

---

## Legacy TOML Config Raises Migration Error During Auto-Discovery

Config auto-discovery (MEDRE_HOME, XDG, local cwd) now checks for
legacy `config.toml` or `medre.toml` files after a YAML file is not
found in the same directory. If a legacy TOML file is present, the
loader raises `ConfigFileError` with the migration message
`"TOML config files are no longer supported; use YAML (.yaml or .yml)."`

Previously a leftover TOML file in a discovery directory was silently
ignored, resulting in a generic `ConfigNotFoundError`. Operators
upgrading from the historical TOML config format now get a clear
migration pointer instead of a confusing "not found" error.

---

## Safe YAML Escaping in Docker Bridge Artifact Output

The redacted `config.yaml` evidence artifact now escapes control
characters and quotes unsafe mapping keys so the output is guaranteed
to be valid round-trippable YAML. `_yaml_escape_string` escapes
newline, carriage return, and tab in addition to backslash and
double-quote. A new `_format_yaml_key` helper double-quotes and
escapes any mapping key that does not match the plain-scalar pattern
`[A-Za-z0-9_][A-Za-z0-9_.-]*` (keys containing `:`, `#`, leading
`-`, trailing spaces, etc.).

Previously config values or keys containing control characters or
YAML-special characters could produce an invalid artifact file.

---

## Remove Matrix TOML Credential Mutation Helpers

Removed dead-code TOML credential helpers from
`src/medre/adapters/matrix/auth.py`:
`update_toml_credentials`, `_update_toml_field`,
`_toml_escape_string`, `_check_section_exists`. These were dead code
after the YAML-only migration; sidecar JSON credentials
(`save_credentials_json`) remain the preferred path. No YAML credential
editor was added. Module docstring updated to drop
"config-file token update" from the feature description.

---

## Per-Context Origin Labels for channel_room_map Entries

Allow each `channel_room_map` entry to carry its own origin labels so
two channels bridged by the same route can show different attribution
text in the relay prefix (for example, the channel name).

**New entry shape:**

Each `channel_room_map` value is now polymorphic. The bare-string shape
(room ID only) is unchanged and carries no per-entry labels. The new
structured shape is a table with `room` plus optional
`source_origin_label` / `dest_origin_label`:

```yaml
routes:
  radio_matrix:
    source_adapters: [main]
    dest_adapters: [ops]
    directionality: bidirectional
    channel_room_map:
      "0":
        room: "!longfast:example.com"
        source_origin_label: "LongFast"
        dest_origin_label: "Matrix Ops"
      "1":
        room: "!shortfast:example.com"
        source_origin_label: "ShortFast"
```

Both shapes can be mixed in the same map.

**Precedence chain** (most to least specific, per expanded leg):

1. Per-entry `source_origin_label` / `dest_origin_label` on the matched
   entry.
2. Route-level `source_origin_label` / `dest_origin_label`.
3. Source adapter `origin_label`.
4. Empty string.

An explicit empty string (`""`) suppresses the fallback below it for
that leg; an unset or absent label falls through to the next level.

**Validation:**

- Unknown keys in a structured entry are rejected.
- Boolean and other non-string label values are rejected (booleans are
  checked before the generic string check, matching route-level label
  validation).
- The bare-string shape, all existing channel-key / duplicate-channel /
  duplicate-room / canonical-room-ID checks, and mutual-exclusion with
  the targeting fields are unchanged.

**Backward compatibility:** every existing `channel_room_map` config
loads identically — bare-string entries still produce the same expansion
with route-level labels applied uniformly.

**Scope:** per-entry labels apply to `channel_room_map` entries only.
General routes (those not using `channel_room_map`) still use one
route-level label pair per route; decompose into separate routes when
the map shape cannot express the targeting you need. `origin_label`
remains human-readable attribution only — not a routing key, not a
transport identity, and not delivery evidence.

---

## Duplicate-Room Fan-In for channel_room_map and Config-Constructor Rename

Allow a `channel_room_map` to map two or more Meshtastic channel indices to
the same Matrix room for Meshtastic→Matrix fan-in, and rename the route
config constructor away from its historical TOML-derived name now that the
runtime is YAML-only.

**Changed:**

- `src/medre/config/routes.py`: duplicate Matrix room values are no longer
  rejected at config parse time. Each `channel_room_map` value still has to
  be a canonical room ID (starting with `!`), and the channel-key,
  duplicate-channel, alias-rejection, and canonical-room-ID checks are
  unchanged.
- `src/medre/runtime/route_engine.py`: new
  `_validate_duplicate_rooms_for_direction` route-level check. After platform
  assignment and directionality are known, it rejects duplicate Matrix rooms
  only when the route's expansion creates a Matrix→Meshtastic leg. A Matrix
  event arriving from a shared room is ambiguous across Meshtastic channels,
  so duplicate rooms are allowed for Meshtastic→Matrix fan-in (the inbound
  radio channel disambiguates the source) and rejected otherwise.
- `src/medre/config/routes.py`, `src/medre/runtime/route_engine.py`:
  `RouteConfig.from_toml_dict` and `RouteConfigSet.from_toml_dict` renamed to
  `from_dict`. The loader is YAML-only and the method names no longer
  reference TOML. Field names and dict shapes are unchanged.

**Directionality decision:** a `channel_room_map` with duplicate rooms is
accepted when no Matrix→Meshtastic leg is created (`source_to_dest` or
`dest_to_source` oriented so only Meshtastic→Matrix expands) and rejected
when a Matrix→Meshtastic leg is created (`source_to_dest` / `bidirectional`
with a Matrix source, or `dest_to_source` / `bidirectional` with a Matrix
destination). A map with no duplicate rooms is always accepted.

**Docs updated:**

- `docs/spec/routing-delivery.md`: new §17.6 documenting the duplicate-room
  fan-in semantics and the directionality decision matrix.
- `docs/ops/configuration.md`: `channel_room_map` limitations now describe
  the fan-in allowance and the Matrix→Meshtastic rejection, with a fan-in
  YAML example.
- `docs/dev/source-context-origin-label-audit.md`,
  `docs/dev/relay-prefix-attribution-audit.md`: updated `from_toml_dict`
  references to `from_dict`, duplicate-room enforcement sites, and stale TOML
  prose.
