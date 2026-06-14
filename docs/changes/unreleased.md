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

**Announce-based LXMF display-name enrichment is not implemented**; a
defensive ingress capture path is in place. Per-channel origin labels
remain unsupported — operators use separate routes per channel.
