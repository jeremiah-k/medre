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

Test evidence is classified into four tiers:

| Tier  | Label      | Meaning                                                 |
| ----- | ---------- | ------------------------------------------------------- |
| **H** | Historical | Recorded during a prior phase. May be stale.            |
| **C** | Current    | Recorded against the current codebase. Reproducible.    |
| **S** | Simulated  | Recorded using fake adapters or mocks. No real network. |
| **R** | Real-live  | Recorded against a real transport endpoint.             |

Simulated evidence MUST NOT be used to support claims about real transport
behavior. Real-live evidence is the only tier that supports claims about
production-adjacent behavior.

## 6. Runtime Conformance Harness

### 6.1 Overview

The runtime conformance harness lives under `tests/conformance/` and asserts
MEDRE runtime contracts — ingress, rendering, capability decisions,
delivery/evidence, and replay — using deterministic JSON fixtures and real
codecs/renderers/services. It does **not** use real SDK network or hardware.

Runtime conformance tests are distinct from:

- **Static schema conformance** — validating JSON payloads against schemas.
- **Pure capability conformance** — testing the `CapabilityDecisionResolver`
  in isolation (covered by `test_capability_decision.py` and
  `test_capability_decision_transport_profiles.py`).
- **Live validation** — testing against real transport endpoints (see §4.4).

### 6.2 Fixture Location and Format

Fixtures live under:

```
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
   correlation, and evidence. Suppressed receipts omit
   `rendering_evidence`. Supplemental queued→sent receipts preserve
   parent, plan, route, channel, and evidence.
5. **Replay**: DRY_RUN skips delivery. BEST_EFFORT applies capability
   filtering. Replay receipts carry `source="replay"` and
   `replay_run_id`.

### 6.5 Conformance Test Modules

| Module                                   | Coverage                                      |
| ---------------------------------------- | --------------------------------------------- |
| `test_ingress_conformance.py`            | Codec decode → CanonicalEvent contracts       |
| `test_rendering_conformance.py`          | Renderer output + RenderingEvidence           |
| `test_capability_runtime_conformance.py` | CapabilityDecisionResolver transport profiles |
| `test_delivery_lifecycle_conformance.py` | Receipt lifecycle and evidence contracts      |
| `test_replay_conformance.py`             | DRY_RUN / BEST_EFFORT parity                  |
