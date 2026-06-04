# Conformance

What it means to conform to the MEDRE specification, test categories, and
authority rules.

See also: [principles.md](principles.md), [architecture.md](architecture.md),
[event-model.md](event-model.md).

---

## 1. Authority Rules

Documents under `docs/spec/` are the authoritative normative specification for
MEDRE. When a `spec/` document conflicts with any other documentation, `spec/`
takes precedence.

- **Operator docs** (`docs/ops/`) describe how to use the runtime; they do not
  define semantics.
- **Developer docs** (`docs/dev/`) describe how to extend the runtime; they do
  not define semantics.
- **Historical planning documents** are not preserved as authoritative
  references.

## 2. RFC 2119 Keywords

Documents under `spec/` use RFC 2119 keywords:

- **MUST** / **MUST NOT** — absolute requirement
- **SHOULD** / **SHOULD NOT** — recommendation unless there is a valid reason
- **MAY** — optional

These keywords MUST NOT appear in `ops/` or `dev/` documentation. Those
directories use plain descriptive language.

## 3. What Conformance Means

An implementation conforms to the MEDRE specification when it satisfies all
MUST and MUST NOT requirements in the spec documents. SHOULD requirements are
recommendations; deviations MUST be documented and justified.

### 3.1 Adapter Conformance

An adapter conforms when it:

1. Implements the `Adapter` protocol (`start`, `stop`, `deliver`, `health_check`).
2. Provides an `AdapterCodec` for native-to-canonical event conversion.
3. Sets `source_transport_id` to the transport's native sender identifier (as
   a string) for all source events.
4. Sets `source_channel_id` to the native channel identifier (or `None` if
   the transport has no channel concept).
5. Never puts private keys, credentials, or configuration in canonical events.
6. Publishes inbound events via `ctx.publish_inbound()`, not by calling other
   adapters.
7. Reports health via `health_check()`.
8. Respects payload limits when embedding envelopes on constrained transports.

### 3.2 Pipeline Conformance

The pipeline conforms when it:

1. Processes events through all stages in order (ingress, dedup,
   resolve_relations, store, route, deliver). See
   [architecture.md §2](architecture.md) for stage descriptions.
2. Never mutates a canonical event after creation.
3. Stores only original events (depth=0). Derived events with
   `parent_event_id` and lineage are reserved for future enrich/transform
   implementation (see [architecture.md §2 — Future Extension Points]).
4. Records delivery receipts for every delivery attempt (append-only).
5. Derives current delivery status from the latest receipt, not by mutating
   receipt rows.
6. Evaluates route policy at the correct stage (after routing, before
   delivery). Delivery-stage policy is a reserved extension point with zero
   current implementation.
7. Supports replay without modifying existing events.

### 3.3 Storage Conformance

A storage backend conforms when it:

1. Implements the `StorageBackend` protocol (`append`, `query`, `get`,
   `append_receipt`, `store_native_ref`, `resolve_native_ref`).
2. Stores canonical events immutably (no update or delete on event rows).
3. Maintains the `native_message_refs` unique constraint on
   `(adapter, native_channel_id, native_message_id)`.
4. Supports the `delivery_status` view as a projection from the latest receipt.

### 3.4 Configuration Conformance

A configuration system conforms when it:

1. Loads TOML configuration via the search order defined in
   [configuration.md](configuration.md).
2. Applies environment variable overrides without mutating the original config.
3. Validates all adapter configs and rejects duplicates.
4. Supports XDG path resolution with `MEDRE_HOME` override.

## 4. Test Categories

### 4.1 Unit Tests

Unit tests verify individual components in isolation using mock/fake
dependencies. They MUST NOT require real network access or hardware.

- Adapter codec round-trips (native event to canonical event and back).
- Policy evaluation correctness.
- Route matching logic.
- Delivery plan construction.
- Event immutability verification.
- Config loading and validation.

### 4.2 Integration Tests

Integration tests verify subsystem interactions using fake adapters. They
exercise the full pipeline without real network traffic.

- Full pipeline: ingress through receipt with fake adapters.
- Route matching with multiple adapters.
- Delivery planning with fallback chains.
- Relation resolution across fake adapters.
- Config loading, env overrides, and runtime assembly.

### 4.3 Adapter-Specific Tests

Tests that exercise adapter-specific behavior with the real SDK but mock
transport endpoints.

- SDK import and initialization.
- Codec correctness against real SDK data types.
- Session lifecycle (connect, reconnect, shutdown).
- Renderer output for transport-specific constraints.

### 4.4 Live Tests

Tests against real transport endpoints (real homeserver, real radio, real
network). These are opt-in, gated by environment variables, and produce
recorded evidence.

- Matrix: Docker Synapse (SDK-boundary) or real homeserver.
- Meshtastic: TCP or serial connection to a physical radio.
- MeshCore: TCP, serial, or BLE connection to a physical node.
- LXMF: Reticulum network connection.

Live tests MUST record the execution date, commit hash, Python version,
environment description, and test outcomes.

### 4.5 Replay Tests

Tests that verify replay behavior:

- Replay produces new derived events and receipts.
- Replay does not modify existing events.
- Replay respects pipeline stage selection.
- Replay supports dry-run mode.

## 5. Evidence Classification

Test evidence is classified into six tiers:

| Tier             | Label        | Meaning                                                               |
| ---------------- | ------------ | --------------------------------------------------------------------- |
| **historical**   | Historical   | Recorded during a prior phase. May be stale.                          |
| **conformance**  | Conformance  | Recorded against the current codebase with deterministic fixtures.    |
| **synthetic**    | Synthetic    | Recorded using fake adapters or mocks. No real network or hardware.   |
| **docker**       | Docker       | Recorded against a local Docker container with real SDK dependencies. |
| **live_service** | Live Service | Recorded against a real external transport service.                   |
| **hardware**     | Hardware     | Recorded against a physical radio device.                             |

Synthetic evidence MUST NOT be used to support claims about real transport behavior. Docker evidence validates SDK integration and adapter wiring, not external network or hardware behavior. Only `live_service` and `hardware` evidence support claims about production-adjacent behavior.

`NOT EXECUTED` (or `not_executed`) is not a tier. It indicates that no evidence of any tier exists. Every `NOT EXECUTED` entry MUST include a `reason` field.

## 6. Runtime Conformance Harness

### 6.1 Overview

The runtime conformance harness lives under `tests/conformance/` and asserts
MEDRE runtime contracts — ingress, rendering, capability decisions,
delivery/evidence, and replay — using deterministic JSON fixtures and real
codecs/renderers/services. It does **not** use real SDK network or hardware.

Runtime conformance tests are distinct from:

- **Static schema conformance** — validating JSON payloads against schemas.
- **Pure capability conformance** — testing the `CapabilityDecisionResolver`
  in isolation (covered by `test_capability_decision_transport_profiles.py`
  and `test_capability_decision.py`).
- **Live validation** — testing against real transport endpoints (see §4.4).

### 6.2 Fixture Location and Format

Fixtures live under:

```text
tests/conformance/fixtures/
├── loader.py            # load_fixture() / load_all_fixtures()
├── matrix/
│   ├── matrix_text_message.json
│   ├── matrix_reply_message.json
│   └── matrix_reaction_message.json
└── meshtastic/
    ├── meshtastic_text_packet.json
    ├── meshtastic_reply_packet.json
    └── meshtastic_reaction_packet.json
```

Each fixture is a self-describing JSON file with these fields:

| Field             | Purpose                                            |
| ----------------- | -------------------------------------------------- |
| `fixture_version` | Schema version (currently `1`).                    |
| `name`            | Human-readable fixture name.                       |
| `adapter`         | Adapter identifier (`"matrix"` or `"meshtastic"`). |
| `description`     | What the fixture exercises.                        |
| `native_input`    | The native dict payload consumed by the codec.     |
| `decode_context`  | Extra kwargs passed to `codec.decode()`.           |
| `expected`        | Assertions about the resulting `CanonicalEvent`.   |

The `expected` block specifies:

- `event_kind` — the expected event kind string.
- `source_adapter`, `source_transport_id`, `source_channel_id`.
- `source_native_ref` — adapter, channel, and message ID.
- `payload_shape` — key-value pairs that must appear in the payload.
- `relations_count` and optionally `first_relation` with type, key,
  and target_native_ref.
- `metadata_has_native` — whether native metadata must be present.

### 6.3 Adding a New Adapter Fixture

To add fixtures for a new adapter (e.g. LXMF, MeshCore):

1. Create `tests/conformance/fixtures/<adapter>/` directory with an
   `__init__.py`.
2. Write JSON fixture files following the format in §6.2.
3. Write ingress conformance tests (or extend
   `test_ingress_conformance.py`) that load fixtures via
   `load_fixture()` or `load_all_fixtures()`, decode through the
   adapter's codec, and assert the expected fields.
4. Add rendering conformance tests if the adapter has a renderer.
5. Run `pytest tests/conformance/ -v` to verify.

### 6.4 What Must Be True for MEDRE Runtime Conformance

An adapter claims MEDRE runtime conformance when the conformance harness
asserts all of the following for its fixtures:

1. **Ingress**: native input decodes to a `CanonicalEvent` with correct
   `event_kind`, `source_native_ref`, `source_adapter`,
   `source_channel_id`, payload shape, relations, and metadata.
2. **Rendering**: canonical events render to native payloads with correct
   envelope fields (e.g. Matrix `msgtype`/`body`/`m.relates_to`,
   Meshtastic `text`/`channel_index`/`meshnet_name`).
3. **Capability decisions**: `CapabilityDecisionResolver` produces
   `direct` for native capabilities, `fallback_text` for fallback,
   and `skip` for unsupported, consistent with transport-profile JSONs.
4. **Delivery lifecycle**: receipts carry correct status, plan
   correlation, and evidence. Service-path tests exercise
   `TargetDeliveryService` with fake adapters and real
   `RenderingPipeline` to verify:
   - Sent receipts: `status == "sent"`, `delivery_plan_id` matches,
     `source == "live"`, canonical `RenderingEvidence` JSON with
     `schema_version`, `renderer`, `delivery_strategy`,
     `target_adapter`, `capability_level`.
   - Queued receipts: `status == "queued"`, `delivery_plan_id` matches,
     canonical `RenderingEvidence` JSON, `target_channel` preserved.
   - Suppressed receipts omit `rendering_evidence`.
   - Supplemental queued→sent receipts preserve parent, plan, route,
     channel, and evidence.
     Shape characterization tests verify receipt field contracts for
     manually-constructed receipts.
5. **Replay**: DRY_RUN skips delivery. BEST_EFFORT applies capability
   filtering through `_filter_plans_by_capability` using real
   `PipelineRunner` with fake adapters — unsupported event kinds are
   filtered to `status="skipped"` with `capability_suppressed` error;
   fallback-capable events remain deliverable. Replay requests carry
   `run_id`; receipt-level `source="replay"` and `replay_run_id`
   tagging is asserted in integration-level tests with real pipeline
   components.

### 6.5 Conformance Test Modules

| Module                                        | Coverage                                                                                                                                                                                  |
| --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_ingress_conformance.py`                 | Codec decode → CanonicalEvent contracts                                                                                                                                                   |
| `test_rendering_conformance.py`               | Renderer output + RenderingEvidence                                                                                                                                                       |
| `test_capability_runtime_conformance.py`      | CapabilityDecisionResolver transport profiles                                                                                                                                             |
| `test_delivery_lifecycle_conformance.py`      | Receipt lifecycle and evidence contracts: shape characterization (manually-constructed receipts) and service-path conformance (TargetDeliveryService + RenderingPipeline + fake adapters) |
| `test_replay_conformance.py`                  | DRY_RUN parity, BEST_EFFORT stub conformance, BEST_EFFORT capability filtering via real PipelineRunner, replay evidence                                                                   |
| `test_evidence_bundle_conformance.py`         | EvidenceBundle assembly conformance: sent, queued, queued→sent, suppressed, replay-origin, invalid rendering_evidence, schema version, deterministic JSON                                 |
| `test_pipeline_live_replay_parity.py`         | Live/replay plan and receipt parity: deterministic plan IDs, strategy equivalence, capability field parity, receipt field matching after normalisation                                    |
| `test_pipeline_suppression_no_send.py`        | Suppression gate conformance: capability skip, loop suppression, plan-level skip never call adapter send; suppressed receipts distinct from failed sends; no retry queue entry            |
| `test_receipt_lineage_retry_parity.py`        | Receipt lineage and retry conformance: delivery_plan_id/route_id/target preservation across retry chain; evidence append semantics; dead_lettered durability                              |
| `test_pipeline_native_ref_loop_prevention.py` | Native ref persistence, dedup, bridge loop prevention, and suppression evidence completeness                                                                                              |
| `test_evidence_operator_diagnostics.py`       | Operator-facing evidence bundle field coverage: all pipeline stages, all status values, capability/strategy/failure traceability, live vs replay source distinction                       |

## 7. Transport Capability Semantics and Delivery Evidence Conformance

This section documents conformance test coverage for transport capability semantics, delivery evidence enrichment, and replay parity introduced alongside transport capability evidence support.

### 7.1 Test Coverage by Behavior

| Behavior                                                                                                                     | Test module(s)                                                                                                                   | Tier      |
| ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | --------- |
| Default `AdapterCapabilities` produce correct capability decisions for known event kinds                                     | `test_capability_decision.py`                                                                                                    | synthetic |
| `None` / missing capabilities fail closed (unsupported/skip)                                                                 | `test_capability_decision.py`                                                                                                    | synthetic |
| Thread relation produces no capability candidate (deferred)                                                                  | `test_capability_decision.py`                                                                                                    | synthetic |
| Transport profile JSONs produce correct decisions (native, fallback, unsupported per adapter)                                | `test_capability_decision_transport_profiles.py`, `test_capability_runtime_conformance.py`                                       | synthetic |
| Relation degradation (reply, reaction, edit, delete) follows three-level semantics                                           | `test_capability_decision.py`, `test_capability_pipeline_enforcement.py`                                                         | synthetic |
| Unknown event kinds pass through (native/direct)                                                                             | `test_capability_decision.py`                                                                                                    | synthetic |
| `thread` produces no relation candidate; unknown non-thread relation types fail closed as unsupported/skip                   | `test_capability_decision.py` (`test_unknown_relation_not_treated_as_thread`, `test_unknown_non_thread_relation_is_unsupported`) | synthetic |
| Capability suppression produces `failure_kind="capability_suppressed"` in delivery outcomes                                  | `test_capability_pipeline_enforcement.py`, `test_delivery_strategy_pipeline_skip.py`                                             | synthetic |
| LXMF renderer enforces `max_text_chars` budget and sets `truncated=True`                                                     | `test_lxmf_renderer.py` (unit)                                                                                                   | synthetic |
| Truncation evidence: `rendered_text_chars`, `original_text_chars` in `RenderingEvidence`                                     | `test_rendering_conformance.py`, `test_lxmf_renderer.py`                                                                         | synthetic |
| Report dict enrichment: `suppression_reason`, `capability_field`, `capability_level`, `delivery_strategy`                    | `test_evidence_suppression.py`                                                                                                   | synthetic |
| `delivery_state_by_target` includes capability-evidence fields (`source`, `replay_run_id`, `suppression_reason`, etc.)       | `test_evidence_target_keyed.py`                                                                                                  | synthetic |
| Evidence bundle carries enriched fields for sent, queued, suppressed, and replay-origin receipts                             | `test_evidence_bundle_conformance.py`                                                                                            | synthetic |
| Replay BEST_EFFORT applies capability filtering via `_filter_plans_by_capability` using real pipeline                        | `test_replay_engine_plan_filters.py`, `conformance/test_replay_conformance.py`                                                   | synthetic |
| All-suppressed replay result includes `capability_suppressed_plans`, `delivery_plan_ids`, `replay_run_id`, `source="replay"` | `test_replay_engine_plan_filters.py`                                                                                             | synthetic |
| Replay capability filtering uses same `CapabilityDecisionResolver` as live delivery                                          | `test_replay_engine_plan_filters.py`                                                                                             | synthetic |
| Deterministic plan IDs via `stable_delivery_plan_id` (same event + route = same plan ID)                                     | `test_pipeline_live_replay_parity.py`                                                                                            | synthetic |
| Live and replay plans are semantically equivalent (plan_id, strategy, capability fields)                                     | `test_pipeline_live_replay_parity.py`                                                                                            | synthetic |
| Live and replay receipts match on core fields (status, failure_kind, delivery_plan_id, target, route)                        | `test_pipeline_live_replay_parity.py`                                                                                            | synthetic |
| Repeated replay runs produce identical plan IDs                                                                              | `test_pipeline_live_replay_parity.py`                                                                                            | synthetic |
| Capability skip suppression does not call adapter send                                                                       | `test_pipeline_suppression_no_send.py`                                                                                           | synthetic |
| Loop suppression does not call adapter send                                                                                  | `test_pipeline_suppression_no_send.py`                                                                                           | synthetic |
| Suppressed receipts have `status="suppressed"` (not `"failed"`) and distinct failure kinds                                   | `test_pipeline_suppression_no_send.py`                                                                                           | synthetic |
| Suppressed deliveries do not enter retry queue                                                                               | `test_pipeline_suppression_no_send.py`, `test_receipt_lineage_retry_parity.py`                                                   | synthetic |
| Retry reconstruction preserves `delivery_plan_id`, `route_id`, `target_adapter`, `target_channel`                            | `test_receipt_lineage_retry_parity.py`                                                                                           | synthetic |
| Retry attempts append new receipts (not overwrite existing)                                                                  | `test_receipt_lineage_retry_parity.py`                                                                                           | synthetic |
| Retry exhaustion produces durable `dead_lettered` evidence                                                                   | `test_receipt_lineage_retry_parity.py`                                                                                           | synthetic |
| Native message refs persisted and resolvable for replay                                                                      | `test_pipeline_native_ref_loop_prevention.py`                                                                                    | synthetic |
| Loop suppression evidence includes `event_id`, `route_id`, `target_adapter`, `failure_kind=LOOP_SUPPRESSED`                  | `test_pipeline_native_ref_loop_prevention.py`                                                                                    | synthetic |
| Operator diagnostics cover all pipeline stages (store, route, plan, render, deliver) in a single evidence bundle             | `test_evidence_operator_diagnostics.py`                                                                                          | synthetic |
| Report dict enrichment includes `delivery_strategy`, `capability_field`, `capability_level`, `suppression_reason`            | `test_evidence_operator_diagnostics.py`                                                                                          | synthetic |

### 7.2 Known Gaps

The following behaviors have synthetic-tier test coverage but lack `live_service` or `hardware` tier validation:

1. **No hardware or live transport validation.** All capability suppression, fallback rendering, and budget enforcement tests use fake adapters and synthetic capability configurations. No test sends a capability-suppressed event to a real Meshtastic radio, Matrix homeserver, or Reticulum LXMF router.

2. **Fallback capability level is dormant in production profiles.** No production transport profile currently declares a capability field at `"fallback"`. The fallback rendering path (`"fallback_text"` strategy, inline text degradation) is tested with synthetic configurations but has never been exercised against a live transport with a real adapter producing degraded output.

3. **RE_RENDER replay mode does not reconstruct a full capability-aware rendering context.** The `RE_RENDER` mode re-runs rendering through the pipeline, but it does not reconstruct `RenderingContext` from stored artifacts. It uses whatever context the replay pipeline provides, which may not match the original rendering context.

4. **Replay pre-filter suppressed evidence is in-memory only.** When `_filter_plans_by_capability` suppresses all plans for an event during replay, the evidence records (`capability_suppressed_plans`, `delivery_plan_ids`, `replay_run_id`) are carried in the in-memory `ReplayResult` output. They are not persisted to storage unless a receipt is created through a different code path. If the process crashes before the operator inspects the replay output, this evidence is lost.

5. **Thread relation capability gating is deferred.** No `AdapterCapabilities.threads` field exists. Thread-carrying events receive native/direct delivery with `capability_field=None` when no other candidate overrides. This is intentional (see Routing and Delivery Specification § 6.3.6) but means thread relations are never capability-suppressed.

6. **`capability_policy` field is reserved and unpopulated.** `RenderingContext.capability_policy` defaults to `None` and is not set by the current pipeline. No test exercises this field.

7. **No live_service or hardware validation for deterministic plan IDs, suppression gates, retry lineage, or operator diagnostics.** All tests use fake adapters and synthetic configurations. No test validates these behaviours against real transport endpoints.

## 8. Deterministic Delivery Plan Identity and Suppression Semantics Conformance

This section documents conformance test coverage for deterministic plan IDs, live/replay parity, suppression semantics, retry lineage, native ref/loop prevention, and operator diagnostics.

### 8.1 Delivery Plan Identity Conformance

A conforming implementation satisfies:

1. **Deterministic plan IDs**: `plan_id` is derived from `event_id`, `route_id`, `target_index`, and a SHA-256 digest of the target identity. It MUST NOT depend on Python object identity (`id()`). Repeated calls with the same inputs produce the same `plan_id`.

2. **Live/replay plan parity**: Live delivery and replay planning produce plans with identical `plan_id`, `route_id`, `target_identity`, `capability_level`, `capability_field`, `capability_reason`, and `primary_strategy.method` for the same event and route configuration.

3. **Receipt parity**: Live and replay receipts for the same event and target match on `event_id`, `delivery_plan_id`, `target_adapter`, `target_channel`, `route_id`, `status`, `error`, `failure_kind`, and `rendering_evidence`. The fields `source`, `replay_run_id`, `receipt_id`, `created_at`, and `adapter_message_id` intentionally differ.

4. **Capability fields populated**: `FallbackResolver.resolve_fallback` populates `capability_level`, `capability_field`, and `capability_reason` from the `CapabilityDecision` on every plan it produces.

### 8.2 Suppression Semantics Conformance

A conforming implementation satisfies:

1. **Adapter not called**: When a suppression guard fires (capability unsupported, self-loop, route-trace cycle), the adapter's `deliver()` method is NOT invoked.

2. **Suppressed status distinct from failed**: Suppressed outcomes have `status="skipped"` and receipts have `status="suppressed"`. These are not `"failed"` or `"transient_failure"` or `"permanent_failure"`. The `failure_kind` is a suppression kind (`LOOP_SUPPRESSED`, `CAPABILITY_SUPPRESSED`, `POLICY_SUPPRESSED`), not an adapter error kind.

3. **Suppressed deliveries not retried**: Suppressed receipts have `next_retry_at=None` and do not appear in `list_due_retry_receipts()`. They are permanently excluded from the retry queue.

4. **Suppression evidence persisted**: Suppressed receipts are persisted in storage and appear in evidence bundles with full `event_id`, `route_id`, `target_adapter`, `failure_kind`, and reason context.

### 8.3 Retry Lineage Conformance

A conforming implementation satisfies:

1. **Identity preservation**: Retry reconstruction preserves `delivery_plan_id`, `route_id`, `target_adapter`, and `target_channel` from the original delivery through the entire retry chain.

2. **Append-only evidence**: Each retry attempt appends a new receipt row. Earlier receipts are not overwritten or deleted.

3. **Dead-lettered durability**: Retry exhaustion produces a `dead_lettered` receipt with `parent_receipt_id` linking to the last failed attempt. This receipt is durable and queryable.

### 8.4 Native Ref and Loop Prevention Conformance

A conforming implementation satisfies:

1. **Native ref persistence**: Inbound and outbound native message refs are persisted to storage and resolvable via `(adapter, native_channel_id, native_message_id)`.

2. **Consistent replay usage**: Native refs are used consistently in replay — `resolve_native_ref` returns the original `event_id` for previously seen triples.

3. **Loop suppression evidence**: Self-loop and route-trace suppression produce outcomes and receipts with `event_id`, `route_id`, `target_adapter`, and `failure_kind=LOOP_SUPPRESSED`. Runtime accounting and route stats reflect `loop_prevented` count.

### 8.5 Operator Diagnostics Conformance

A conforming implementation satisfies:

1. **Pipeline stage coverage**: A single evidence bundle for a fully-processed event contains data from all five pipeline stages (store, route, plan, render, deliver).

2. **Report dict enrichment**: Every receipt report dict includes derived fields: `delivery_strategy`, `capability_field`, `capability_level`, `suppression_reason`, `failure_kind_detail`, and `retryable`.

3. **Live/replay distinction**: Evidence bundles distinguish live from replay deliveries via `source` and `replay_run_id` fields. When both exist for the same event, separate entries are visible.

### 8.6 Known Gaps

All conformance tests in §8 use synthetic-tier evidence (fake adapters). No `live_service` or `hardware` tier validation exists for:

1. Deterministic plan ID generation against real transport endpoints.
2. Suppression gate behaviour with real adapters.
3. Retry lineage preservation across process restart with real adapters.
4. Native ref persistence and replay consistency with real adapters.
5. Operator diagnostics completeness with real transport data.

## 9. Recovery Convergence Diagnostics Conformance

### 9.1 Convergence Classification Conformance

A conforming implementation satisfies:

1. **Pure diagnostics**: `build_convergence_summary()` and `build_orphan_report()` are pure functions with no I/O, no state mutation, and no storage access.

2. **Closed severity vocabulary**: Convergence severity values are exactly `safe`, `degraded`, `inconsistent`. No other values are valid.

3. **Deterministic target grouping**: Targets are grouped by `(delivery_plan_id, target_adapter, target_channel)` with deterministic tie-breaking.

4. **Deterministic receipt selection**: The latest receipt is selected by `(attempt_number DESC, sequence DESC, created_at DESC, receipt_id DESC)` without relying on object identity.

5. **Detection-only policy**: The diagnostics system does not repair state, block startup, or perform automatic remediation.

### 9.2 Orphan Finding Kinds Conformance

A conforming implementation detects exactly ten finding kinds: `orphaned_outbox`, `orphaned_parent_receipt`, `cross_plan_parent`, `cross_event_parent`, `missing_delivery_plan_id`, `dead_lettered_retryable_mismatch`, `recovered_not_progressed`, `repeatedly_reclaimed`, `reclaimed_then_terminal`, `reclaimed_then_orphaned`. No other finding kinds are valid.

### 9.3 Evidence Bundle Integration Conformance

1. The `EvidenceCollector` populates `convergence_summary` on each per-event `EvidenceBundle` from the event's receipts and outbox items.
2. The `convergence_summary` field is JSON-safe and deterministic for identical inputs.
3. The JSON schema for `EvidenceBundle` accepts `convergence_summary` as an optional `ConvergenceSummary` (see `evidence-bundle.schema.json`).

### 9.4 Replay/Live Separation Conformance

1. Queued callback source selection prefers non-replay (`"live"`, `"retry"`) candidates over `"replay"` candidates when multiple matching queued receipts exist.
2. When only replay candidates are available, the pipeline skips correlation and emits a warning. No supplemental sent receipt is created. Replay-only queued receipts MUST NOT be used for callback correlation because `OutboundNativeRefRecord` carries no trusted `source` / `replay_run_id` provenance. This restriction MAY be relaxed in a future version when callback records carry trusted replay provenance.
3. Replay does not mutate live recovery state (receipts, outbox items, retry state).

### 9.5 Startup Ownership Conformance

1. Startup reclaims non-terminal outbox items lazily through `claim_due_outbox_items()`, not by a startup-time state sweep.
2. Startup does not block on convergence diagnostics.
3. Terminal outbox statuses require no startup action.

### 9.6 Recovery Ownership Conformance

A conforming implementation satisfies:

1. **Observable startup ownership**: Every recovery or diagnostic ownership classification carries a `RecoveryOwnershipAction` in a `StartupRecoveryLedger`. The `RecoverySummary` provides deterministic totals with consistency validation (`total_items == sum` of all categories). Ownership statuses MUST be exactly six: `recoverable`, `claimed_for_recovery`, `reclaimed`, `abandoned`, `unrecoverable`, `skipped`.

2. **Attributable recovery actions**: Every recovery or diagnostic ownership classification MUST carry a `recovery_source` field identifying which subsystem reclaimed ownership (`startup_recovery`, `retry_worker_recovery`, `snapshot_diagnostics`, or `replay_execution`). The value `replay_execution` is reserved for future replay recovery ownership actions and MUST NOT be produced by current implementations. The value `snapshot_diagnostics` is used for diagnostic classification from stored outbox/receipt snapshots where no runtime startup or retry worker performed actual recovery. Every action MUST carry `recovery_run_id`, `outbox_id`, `prior_status`, `observed_status`, `ownership_action`, `reason`, and `worker_identity` (when available).

3. **Append-only evidence**: The `StartupRecoveryLedger` is append-only. Recovery actions are deterministically ordered by `(outbox_id, timestamp)`. Once recorded, actions SHALL NOT be removed or modified.

4. **Recovery diagnostics are read-only**: Classification and builder functions SHALL NOT mutate outbox items or receipts. They SHALL NOT perform I/O or access storage.

5. **Replay is not recovery**: `recovery_source="replay_execution"` is reserved for future replay recovery ownership actions. Replay-origin recovery SHALL NOT be conflated with startup or retry-worker recovery. Current replay separation is represented by replay receipts (source/replay_run_id), not by recovery ownership actions. Startup recovery diagnostics MUST NOT classify replay-sourced activity as startup or retry-worker recovery.

6. **Recovery is not proof of delivery**: Recovery actions document outbox transitions, not delivery confirmations. The `ownership_action` values reference claim statuses, not delivery statuses. Terminal outbox statuses (`sent`, `dead_lettered`) are classified as `unrecoverable` and SHALL NOT be presented as requiring recovery.

7. **Recovery convergence visibility**: The convergence diagnostics system SHALL detect four recovery-accountability patterns: `recovered_not_progressed`, `repeatedly_reclaimed`, `reclaimed_then_terminal`, `reclaimed_then_orphaned`. These findings SHALL be merged into the existing `OrphanReport` and follow the same detection-only policy.

8. **Deterministic startup reporting**: The evidence bundle SHALL include a `recovery` section when collected with a runtime configuration. The section SHALL contain a `recovery_summary` and `recovery_ledger` with deterministic JSON output for identical inputs. The per-event `EvidenceBundle` SHALL carry `recovery_summary` and `recovery_ledger` as optional fields.

9. **Schema coverage**: Recovery evidence SHALL be accepted by the `evidence-bundle.schema.json` JSON Schema. JSON examples SHALL exist for `recovery-summary` and `recovery-ledger`.

10. **Recovery evidence is diagnostic, not transactional**: Recovery evidence is generated as bundle snapshots from stored state at collection time. It is not an append-only log of live recovery transactions. Evidence collection does not perform actual startup recovery. Three distinct semantic tiers exist (see diagnostics-evidence.md § 22.3.1): actual startup recovery (real `recovery_run_id` from boot), runtime snapshot diagnostics (snapshot-scoped `recovery_run_id`), and per-event diagnostics (`recovery_run_id=None`). These tiers MUST NOT be conflated.

### 9.7 Known Gaps

All convergence diagnostics and recovery ownership tests use synthetic-tier evidence (fake adapters and mock data). No `live_service` or `hardware` tier validation exists for:

1. Recovery ownership classification against real restart cycles with real adapters.
2. Recovery convergence findings against real outbox/receipt state with real transport data.
3. Startup recovery ledger across actual process restart cycles.
4. Recovery source disambiguation with concurrent retry workers and replay sessions.

## 10. Lifecycle Delivery Convergence Diagnostics Conformance

### 10.1 Lifecycle Convergence Conformance

A conforming implementation satisfies:

1. **Pure diagnostics**: `build_lifecycle_convergence_findings()` is a pure function with no I/O, no state mutation, and no storage access. It does not change retry scheduling, worker behavior, or delivery state.

2. **Closed finding-kind vocabulary**: Lifecycle convergence finding kinds are exactly nine: `receipt_outbox_mismatch`, `terminal_receipt_nonterminal_outbox`, `terminal_outbox_nonterminal_receipt`, `retry_wait_missing_next_retry`, `next_retry_in_past`, `retryable_without_retry_metadata`, `stalled_delivery_plan`, `attempt_count_regression`, `receipt_sequence_gap`. No other finding kinds are valid.

3. **Closed severity vocabulary**: Lifecycle convergence severity values are exactly `degraded` and `inconsistent`. The `safe` value is included in `severity_counts` for structural parity but no finding is produced with `safe` severity.

4. **Severity assignment correctness**: Finding kinds classified as `inconsistent` MUST be: `terminal_outbox_nonterminal_receipt`, `retry_wait_missing_next_retry`, `attempt_count_regression`. Finding kinds classified as `degraded` MUST be: `terminal_receipt_nonterminal_outbox`, `receipt_outbox_mismatch`, `next_retry_in_past`, `retryable_without_retry_metadata`, `stalled_delivery_plan`, `receipt_sequence_gap`.

5. **Deterministic target grouping and finding selection**: Targets are grouped by `(delivery_plan_id, target_adapter, target_channel)` using the same deterministic tie-breaking as convergence summary (§9.1). Findings are sorted deterministically by `(kind, record_id)`.

6. **Detection-only policy**: The lifecycle convergence diagnostics system does not repair state, block startup, change retry scheduling, change worker behavior, or perform automatic remediation. It does not write to storage under any circumstances.

7. **Read-only guarantee**: Lifecycle convergence diagnostics do not mutate outbox items, receipts, or any runtime state. They are pure projections from already-loaded snapshots.

### 10.2 Evidence Bundle Integration Conformance

1. The `EvidenceCollector` populates `lifecycle_convergence_report` on each per-event `EvidenceBundle` from the event's receipts and outbox items.
2. The `lifecycle_convergence_report` field is JSON-safe and deterministic for identical inputs and clock values.
3. The JSON schema for `EvidenceBundle` accepts `lifecycle_convergence_report` as an optional `LifecycleConvergenceReport` (see `evidence-bundle.schema.json`).
4. The runtime storage evidence section includes `lifecycle_convergence_report` in its data payload.

### 10.3 Relationship to Other Convergence Reports

The `lifecycle_convergence_report` is distinct from `convergence_summary` and `orphan_report`:

- `convergence_summary` classifies overall per-target state (safe/degraded/inconsistent) based on outbox/receipt status agreement.
- `orphan_report` detects orphaned records, invalid lineage, and recovery-accountability patterns.
- `lifecycle_convergence_report` detects specific lifecycle contradictions: status mismatches, retry metadata anomalies, stalled plans, attempt regressions, and sequence gaps.

All three reports are populated from the same receipts and outbox items during evidence collection. They are complementary, not redundant.

### 10.4 Known Gaps

All lifecycle convergence diagnostics tests use synthetic-tier evidence. No `live_service` or `hardware` tier validation exists for:

1. Lifecycle convergence findings against real outbox/receipt state with real transport data.
2. Stall threshold accuracy under real delivery latency conditions.
3. Sequence gap detection with real adapter callback timing.
