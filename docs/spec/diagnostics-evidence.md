# Diagnostics and Evidence Specification

> **Status:** Active
> **Classification:** Normative
> **Authority:** Authoritative specification for diagnostics shape, runtime snapshots, evidence classification, and evidence collection
> **Last reviewed:** 2026-05-27

This document defines the normative contract for the MEDRE diagnostics subsystem, runtime snapshot layer, and operational evidence collection. Every runtime component, adapter, test harness, and operator tool that produces or consumes diagnostic data or evidence MUST conform to this specification.

The key words "MUST", "MUST NOT", "REQUIRED", "SHALL", "SHALL NOT", "SHOULD", "SHOULD NOT", "RECOMMENDED", "MAY", and "OPTIONAL" in this document are to be interpreted as described in RFC 2119.

## 1. Scope

This specification governs:

- Common and per-adapter diagnostic key shapes.
- The `RuntimeSnapshot` dataclass and its serialization contract.
- Health normalization vocabulary and provenance semantics.
- Evidence bundle structure, classification, and provenance levels.
- Sanitization guarantees for all diagnostic and evidence paths.
- Evidence boundary definitions (live, docker, hardware).
- Limitations of what evidence can and cannot prove.
- Beta contractual guarantees.

## 2. Common Diagnostic Keys

Every adapter exposes `health_check()` returning `AdapterInfo` and `diagnostics()` returning a plain dict. The following eight keys SHALL appear in the `diagnostics()` output of all four adapter families:

| Key                           | Type          | Semantics                                                                               |
| ----------------------------- | ------------- | --------------------------------------------------------------------------------------- |
| `connected`                   | `bool`        | Transport connection state. MAY appear directly or nested in a session sub-dict.        |
| `health`                      | `str`         | One of the six health vocabulary strings (see § 5).                                     |
| `mode`                        | `str`         | Transport mode: `"fake"`, `"tcp"`, `"serial"`, `"ble"`, or `"reticulum"` as applicable. |
| `reconnecting`                | `bool`        | `true` when an active reconnect loop is in progress.                                    |
| `reconnect_attempts`          | `int`         | Current reconnect attempt count, bounded to a maximum of 10.                            |
| `last_error`                  | `str or None` | `str()` of the last exception encountered. `None` when no error has occurred.           |
| `transient_delivery_failures` | `int`         | Cumulative count of transient delivery failures since adapter start.                    |
| `permanent_delivery_failures` | `int`         | Cumulative count of permanent delivery failures since adapter start.                    |

**Matrix note:** `last_error` appears as `last_sync_error` within the session diagnostics dataclass.

**Meshtastic/MeshCore note:** Session-level diagnostics are exposed via a `session` sub-dict within the adapter-level diagnostics dict.

**LXMF note:** Session diagnostics are exposed directly via the `LxmfSessionDiagnostics` frozen dataclass. The LXMF adapter does not layer its own outer diagnostics dict on top.

These eight keys are contractual for the current version. They SHALL NOT be removed or have their types changed without a version bump.

## 3. Per-Adapter Diagnostic Keys

Adapter-specific keys convey transport-unique state beyond the common set. Shape and keys vary by adapter transport. New transport-specific diagnostic keys MAY be added. Existing keys SHALL NOT be removed or have their types changed without a version bump.

> **Note:** Transport profiles define the complete per-adapter diagnostic key set. The tables below show the minimum contractual keys present in all adapter implementations. Key counts may differ from transport profiles, which define additional transport-specific keys.

### 3.1 Matrix (21 keys)

| Key                         | Type            | Semantics                                                   |
| --------------------------- | --------------- | ----------------------------------------------------------- |
| `logged_in`                 | `bool`          | nio login restoration state                                 |
| `sync_task_running`         | `bool`          | Background sync loop alive                                  |
| `store_path_configured`     | `bool`          | E2EE crypto store path present                              |
| `device_id_configured`      | `bool`          | E2EE device ID present                                      |
| `encryption_mode`           | `str`           | One of: `"plaintext"`, `"e2ee_optional"`, `"e2ee_required"` |
| `crypto_enabled`            | `bool`          | vodozemac loaded and crypto active                          |
| `last_crypto_error`         | `str or None`   | Last E2EE failure reason                                    |
| `encrypted_room_seen`       | `bool`          | At least one encrypted room encountered                     |
| `undecryptable_event_count` | `int`           | Messages that failed decryption                             |
| `sync_running`              | `bool`          | Sync loop state                                             |
| `last_successful_sync`      | `float or None` | Epoch timestamp of last successful sync                     |
| `crypto_store_loaded`       | `bool`          | Crypto database loaded (olm and store both present)         |
| `encrypted_room_count`      | `int`           | Count only. No room IDs exposed.                            |
| `plaintext_room_count`      | `int`           | Count only. No room IDs exposed.                            |
| `olm_loaded`                | `bool`          | nio Olm machine is initialized                              |
| `store_loaded`              | `bool`          | nio SQLite crypto store is loaded                           |
| `device_keys_uploaded`      | `bool`          | `should_upload_keys` is False (keys present on server)      |
| `key_query_needed`          | `bool`          | Outstanding device key queries pending                      |
| `device_id_in_use`          | `str or None`   | Actual device_id in use (for identity verification)         |
| `store_path_exists`         | `bool`          | Store directory exists on disk                              |
| `initial_sync_completed`    | `bool`          | First successful full_state sync completed                  |

### 3.2 Meshtastic (adapter-level: 7 keys; session sub-dict: 3 keys)

Adapter-level keys:

| Key                  | Type  | Semantics                                 |
| -------------------- | ----- | ----------------------------------------- |
| `adapter_id`         | `str` | Adapter identifier                        |
| `platform`           | `str` | Always `"meshtastic"`                     |
| `connection_type`    | `str` | `"fake"`, `"tcp"`, `"serial"`, or `"ble"` |
| `queue_pending`      | `int` | Current outbound queue depth              |
| `queue_total_sent`   | `int` | Lifetime successful sends via queue       |
| `queue_total_failed` | `int` | Lifetime failures via queue               |
| `background_tasks`   | `int` | Tracked asyncio tasks                     |

Session sub-dict keys (`session.*`):

| Key                        | Type            | Semantics                     |
| -------------------------- | --------------- | ----------------------------- |
| `session.node_id`          | `str or None`   | Local node number             |
| `session.channel_count`    | `int`           | Configured channels           |
| `session.last_packet_time` | `float or None` | Epoch of last received packet |

### 3.3 MeshCore (5 keys)

| Key                 | Type          | Semantics                                 |
| ------------------- | ------------- | ----------------------------------------- |
| `adapter_id`        | `str`         | Adapter identifier                        |
| `platform`          | `str`         | Always `"meshcore"`                       |
| `mode`              | `str`         | `"fake"`, `"tcp"`, `"serial"`, or `"ble"` |
| `last_message_time` | `str or None` | ISO 8601 timestamp                        |
| `peer_count`        | `int or None` | Known mesh peers                          |

### 3.4 LXMF (6 keys)

| Key                      | Type           | Semantics                            |
| ------------------------ | -------------- | ------------------------------------ |
| `router_running`         | `bool`         | LXMRouter is active                  |
| `last_message_time`      | `str or None`  | ISO 8601 timestamp                   |
| `known_path_count`       | `int or None`  | Reticulum path table entries         |
| `propagation_enabled`    | `bool or None` | LXMF propagation node state          |
| `pending_delivery_count` | `int or None`  | Outbound deliveries not yet terminal |
| `mode`                   | `str`          | `"fake"` or `"reticulum"`            |

## 4. RuntimeSnapshot Dataclass

The `RuntimeSnapshot` frozen dataclass aggregates runtime state into an immutable, JSON-safe snapshot. It is constructed by the `capture_runtime_snapshot()` pure function.

### 4.1 Fields (9)

| Field                    | Type                         | Default                     | Semantics                                        |
| ------------------------ | ---------------------------- | --------------------------- | ------------------------------------------------ |
| `adapters`               | `tuple[dict[str, Any], ...]` | —                           | Sorted list of normalized adapter health dicts   |
| `renderer_registry`      | `dict[str, Any]`             | —                           | Status summary from the rendering pipeline       |
| `event_bus_status`       | `dict[str, Any]`             | —                           | Status summary from the event bus                |
| `storage_backend_status` | `dict[str, Any]`             | —                           | Storage backend status or unavailable sentinel   |
| `replay_backend_status`  | `dict[str, Any]`             | —                           | Replay backend status or unavailable sentinel    |
| `route_topology`         | `dict[str, Any]`             | `{"status": "unavailable"}` | Topology-aware route diagnostics from the Router |
| `queue_status`           | `dict[str, str]`             | `{"status": "unavailable"}` | Queue subsystem status                           |
| `backpressure_status`    | `dict[str, str]`             | `{"status": "unavailable"}` | Backpressure subsystem status                    |
| `task_status`            | `dict[str, str]`             | `{"status": "unavailable"}` | Task subsystem status                            |

### 4.2 Construction Contract

`capture_runtime_snapshot()` is a pure function. It SHALL NOT:

- Start polls or health checks.
- Trigger state changes in any supplied object.
- Perform I/O or call async methods.
- Modify any supplied object.

Adapter entries are sorted by `adapter_id` before inclusion. Subsystems that are not provided or unavailable receive `{"status": "unavailable"}` sentinel values.

### 4.3 Snapshot Scope

The `build_runtime_snapshot()` function in the runtime snapshot builder extends the diagnostics snapshot into a comprehensive 17-section shape. The top-level `snapshot_scope` field indicates capture context:

| Value     | Meaning                                                                                    |
| --------- | ------------------------------------------------------------------------------------------ |
| `"build"` | Build-time snapshot. Runtime was built and possibly started for diagnostics, then stopped. |
| `"live"`  | Live-started runtime. `refresh_live_health()` was called to poll current adapter state.    |

## 5. Health Normalization Vocabulary

`normalize_adapter_health()` projects `AdapterInfo` and optional `AdapterState` into a JSON-safe dict. The `health` field is constrained to exactly six strings:

| Value      | Semantics                                                     |
| ---------- | ------------------------------------------------------------- |
| `healthy`  | Adapter is connected and operating normally.                  |
| `degraded` | Adapter is connected but experiencing transient errors.       |
| `failed`   | Adapter is in a non-recoverable failure state. Not connected. |
| `unknown`  | Health state has not been determined.                         |
| `starting` | Adapter is in the process of starting up.                     |
| `stopping` | Adapter is in the process of shutting down.                   |

This is a read-only projection. The normalization layer SHALL NOT add health polling, circuit breakers, or auto-degrade logic.

The normalized output dict contains `adapter_id`, `platform`, `health`, `mode`, and optionally `capabilities` and `details`.

## 6. Deterministic Serialization Contract

### 6.1 Recursive Key Sorting

Every dict in the snapshot — top-level, section internals, adapter entries, route entries, event details, nested sub-dicts — MUST have keys in alphabetical sorted order. This is enforced by `_sorted_dict()` at every level.

`json.dumps(snapshot, sort_keys=True)` MUST produce identical output for identical runtime state with identical clock inputs.

### 6.2 Adapter-Level Serialization

Adapters that return plain dicts from `diagnostics()` (Matrix, Meshtastic, MeshCore) do not enforce key ordering themselves. Deterministic ordering is the responsibility of the `RuntimeSnapshot.to_dict()` layer when adapter diagnostics are aggregated. Individual adapter `diagnostics()` output MAY have arbitrary key order.

### 6.3 JSON-Safety

Every value in the snapshot and evidence output MUST be one of: `dict`, `list`, `str`, `int`, `float`, `bool`, `None`. No SDK objects, no custom types, no secrets. `json.dumps()` MUST succeed without a custom encoder.

### 6.4 Boundedness

Collections in the snapshot are capped:

| Collection           | Cap            |
| -------------------- | -------------- |
| Adapter entries      | 256            |
| Route entries        | 1024           |
| Build failures       | 64             |
| Error strings        | 512 characters |
| Event detail strings | 256 characters |
| Runtime events       | 256 entries    |

When a collection exceeds its cap, entries beyond the cap (sorted for adapters/routes, FIFO for events) are silently excluded.

## 7. Evidence Bundle Structure

The `collect_evidence_bundle()` function assembles a comprehensive evidence bundle with the following top-level shape:

| Key               | Type          | Semantics                                                   |
| ----------------- | ------------- | ----------------------------------------------------------- |
| `schema_version`  | `int`         | Currently `1`. Frozen during pre-release.                   |
| `status`          | `str`         | Overall status: `"passed"`, `"partial"`, or `"error"`.      |
| `sections`        | `dict`        | Per-section evidence data (see § 7.1).                      |
| `errors`          | `list[str]`   | Accumulated error strings from section collection.          |
| `limitations`     | `list[str]`   | Fixed list of evidence limitations (see § 7.2).             |
| `collected_at`    | `str`         | ISO 8601 timestamp of collection.                           |
| `generated_at`    | `str`         | ISO 8601 timestamp of bundle generation.                    |
| `command`         | `str`         | Always `"evidence"`.                                        |
| `config_source`   | `str or None` | Config discovery source. `None` when config loading fails.  |
| `medre_version`   | `str`         | MEDRE package version string.                               |
| `runtime_started` | `bool`        | Whether the runtime was started during evidence collection. |

### 7.1 Sections

Each section follows the pattern `{"status": str, "error": str or None, "data": Any or None}`.

| Section                | Statuses                             | Semantics                                                                    |
| ---------------------- | ------------------------------------ | ---------------------------------------------------------------------------- |
| `config_summary`       | `"passed"`, `"error"`                | Loaded config metadata, adapter counts, route counts.                        |
| `route_validation`     | `"passed"`, `"partial"`, `"error"`   | Route eligibility validation results.                                        |
| `diagnostics_snapshot` | `"passed"`, `"error"`                | Build-time diagnostics snapshot (no runtime start).                          |
| `live_health`          | `"passed"`, `"partial"`, `"skipped"` | Live adapter health after `refresh_live_health()`. Skipped unless requested. |
| `storage`              | `"passed"`, `"partial"`, `"error"`   | Storage backend evidence: receipts, incident summaries, outbox state.        |

Status computation:

- All sections `"passed"` or `"skipped"` → overall `"passed"`.
- Any section `"partial"` or mixed `"error"`/`"skipped"` → overall `"partial"`.

### 7.2 Fixed Limitations

The evidence bundle always includes these limitation statements:

1. Evidence is a point-in-time snapshot, not continuous monitoring.
2. Diagnostics snapshot reflects build-time state unless `--include-refresh-health` is used.
3. Storage section requires an existing initialized database.
4. Fake adapters report synthetic health, not real transport connectivity.
5. No sustained throughput, reconnection resilience, or load evidence.

## 8. Evidence Classification and Provenance Levels

All operational evidence MUST be classified into exactly one of four tiers. The tier determines what claims MAY be derived from the evidence.

### 8.1 Tier Definitions

| Tier  | Label                    | Semantics                                                                                                                      | Allowed Claims                                                                                       |
| ----- | ------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------- |
| **H** | Historical               | Recorded during a prior development phase. Not re-confirmed against the current codebase.                                      | "On date D, behavior X was observed." No claim about current behavior.                               |
| **C** | Current                  | Recorded against the current codebase. Reproducible by re-running the same command at the same commit.                         | "At commit H, behavior X is confirmed."                                                              |
| **S** | Simulated / Fake-runtime | Recorded using `FakeAdapter`, mock objects, or simulated transport. No real network or hardware involved.                      | "The adapter's internal logic produces X when given input Y." No claim about real endpoint behavior. |
| **R** | Real-live-runtime        | Recorded against a real transport endpoint with real network or hardware. Requires env vars, SDK, and physical/network access. | "Against real endpoint E, behavior X was observed under conditions Y."                               |

### 8.2 Classification Rules

1. Every evidence table entry MUST include a `tier` field with value `H`, `C`, `S`, or `R`.
2. Historical evidence MUST include the original recording date. It MUST NOT be presented as current.
3. Simulated evidence MUST NOT be used to support claims about real transport behavior.
4. Real-live-runtime evidence is the ONLY tier that supports claims about production-adjacent behavior.
5. `NOT EXECUTED` is not a tier. It is an explicit statement that no evidence of any tier exists. Every `NOT EXECUTED` entry MUST include a `reason` field.

### 8.3 Tier Transitions

Historical evidence (H) MAY be upgraded to current (C) or real-live-runtime (R) by re-running the corresponding test at the current commit. The upgrade MUST include the new date, commit, and full evidence fields.

Simulated evidence (S) SHALL NOT be upgraded to R without a real endpoint run.

### 8.4 Provenance Metadata

Each section in the runtime snapshot carries explicit provenance:

| Field          | Values                                              | Semantics                                             |
| -------------- | --------------------------------------------------- | ----------------------------------------------------- |
| `scope`        | `"build"`, `"startup"`, `"process_local"`, `"live"` | When the data was captured or computed.               |
| `live_refresh` | `bool`                                              | Whether MEDRE actively polled external adapter state. |

Scope semantics:

- **`build`**: Computed once during `MedreApp.build()`. Frozen after build.
- **`startup`**: Computed once during `MedreApp.start()`. Frozen after startup.
- **`process_local`**: In-memory state at snapshot time. Not persisted across restarts.
- **`live`**: Actively polled from external adapters via `health_check()`.

## 9. Sanitization Guarantees

### 9.1 No Secret Leakage

No adapter, snapshot, or evidence path SHALL expose access tokens, private keys, identity material, authentication credentials, device keys, crypto material, or session keys through any diagnostic or evidence path. This applies to all log levels, including DEBUG.

| Adapter    | Guarantee                                                                    | Mechanism                                                                  |
| ---------- | ---------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Matrix     | No secrets, access tokens, keys, or private device material                  | Frozen dataclass; token/key fields never included; room names/IDs excluded |
| Meshtastic | No secrets, private keys, raw protobuf dumps, or sensitive radio identifiers | Frozen dataclass; node_id is public info; no packet payloads               |
| MeshCore   | No secrets, private keys, or raw SDK internals                               | Plain dict copy; no pubkey material                                        |
| LXMF       | No secrets, private keys, identity material, or unsafe peer dumps            | Frozen dataclass; identity hashes not included; mode is string             |

### 9.2 No SDK Object Leakage

No adapter SHALL expose the underlying SDK client object, connection handle, or crypto material through diagnostics. Specifically prohibited: protobuf objects, `LXMessage` instances, nio client references, `Event` objects, RNS Identity/Link/Destination object representations.

### 9.3 No Binary Wire Formats

All exceptions MUST be converted to `str()` before inclusion. All complex objects MUST be reduced to plain dicts with JSON-safe types.

### 9.4 Sanitization Enforcement

Error strings in the snapshot are passed through `_sanitize_error()` which truncates at 512 characters and strips secret patterns (tokens, API keys, passwords). Adapter configs are never introspected. Sanitization applies uniformly across all snapshot and evidence output paths.

## 10. Evidence Boundaries

### 10.1 Live Boundary

Live evidence (R-tier) is collected against real transport endpoints with real network connectivity. This boundary requires:

- Running transport infrastructure (Matrix homeserver, Meshtastic node, MeshCore node, or Reticulum network).
- Valid authentication credentials (not recorded in evidence).
- SDK dependencies installed and functional.

Live evidence is the ONLY boundary that supports claims about production-adjacent behavior. It is process-scoped and reflects observations made by the local MEDRE process against real endpoints.

### 10.2 Docker/Container Boundary

Container boundary evidence validates deployment isolation. It confirms that:

- Deployment helpers have no SDK imports or instantiation.
- CLI modules have no top-level SDK imports and use dynamic probing only.
- Snapshot and export modules have no transport SDK coupling.
- Clean-env test files import no transport SDKs.
- Fake-only test files have no SDK imports; live test files carry appropriate markers.
- Live tests are excluded from default test execution.

Container evidence is S-tier (deterministic test pass/fail). It does not require live endpoints.

### 10.3 Hardware Boundary

Hardware evidence is collected when a physical radio device is connected (Meshtastic node, MeshCore node). It requires:

- Physical device connected via serial, TCP, or BLE.
- Appropriate firmware version on the device.
- Device-specific configuration (channel index, channel name, etc.).

Hardware evidence is R-tier when the device is present and responding. It captures hardware/firmware snapshots, connection establishment times, and send/receive behavior against the physical radio. No hardware evidence exists when no physical device is available.

### 10.4 Fake-Only Boundary

Fake-adapter evidence (S-tier) uses `FakeAdapter` and simulated transport. It validates internal logic without any network or hardware dependency. S-tier evidence MUST NOT be used to support claims about real transport behavior.

## 11. What Evidence Cannot Prove

The following claims are prohibited without explicit R-tier evidence:

1. **Reliability:** "Transport X reliably delivers messages." Requires R-tier sustained operation evidence.
2. **Failure recovery:** "Transport X recovers from network failures." Requires R-tier reconnect evidence.
3. **E2EE security:** E2EE security is an upstream nio/vodozemac property. It is not a MEDRE claim regardless of evidence tier.
4. **Production readiness:** No transport qualifies as production-ready. This claim is prohibited.
5. **Ordering:** "Messages are delivered in order." No evidence supports ordering claims.
6. **Latency bounds:** "Delivery latency is bounded by X ms." Requires R-tier evidence with timing measurements.
7. **Start/stop safety:** "Repeated start/stop is safe in production." Requires R-tier start/stop cycle evidence.
8. **Boundedness under load:** "Boundedness guarantees hold under load." Requires R-tier sustained operation evidence.

Additionally, delivery evidence has explicit non-guarantees:

- **Matrix `tx_id`** reduces duplicate retries but is not exactly-once. The homeserver may have already processed and lost a prior attempt, or the deduplication window may have expired.
- **Meshtastic queue acceptance** is not RF confirmation. A `queued` or `sent` receipt means the local node accepted the packet. No remote node acknowledgement is available.
- **Meshtastic classifier counters** are aggregate, not per-packet records. They do not persist a log of every individual ignored, dropped, or deferred packet. They reset on adapter restart.

## 12. Observational-Only Caveat

**Diagnostics are snapshot observations, not authoritative state.**

This applies to all diagnostic paths: adapter `diagnostics()`, session diagnostics dataclasses, `RuntimeSnapshot`, and evidence bundle sections. The implications:

1. `connected: true` does not guarantee the next operation will succeed. The transport MAY disconnect between the snapshot and the next operation.
2. `reconnect_attempts: 0` does not mean the connection is stable. It means no reconnect loop is currently running.
3. Delivery failure counters are cumulative since adapter start, not per-message receipts. The delivery receipt pipeline is the authoritative source for delivery state.
4. Diagnostics are not a substitute for delivery receipts. They serve operational monitoring and debugging only.
5. The `RuntimeSnapshot` is frozen at capture time. It SHALL NOT update if underlying state changes after construction.
6. Startup-derived health values (`adapters.{id}.health`, `startup.startup_health`) do not reflect post-startup state changes. If an adapter crashes after startup, these values will still show the startup-time state.
7. Runtime event buffers are in-memory only and not persisted across restarts.

## 13. Beta Contractual Guarantees

The following six guarantees are contractual for the current beta period:

1. **No secret leakage through any diagnostic path.** Frozen dataclasses and explicit sanitization enforce this. Turning up log verbosity MUST NOT leak secrets.

2. **No SDK object leakage.** Verified across all four adapters. No protobuf objects, no `LXMessage` instances, no nio client references, no `Event` objects leak through any diagnostic or evidence path.

3. **Deterministic serialization when consumed through `RuntimeSnapshot.to_dict()`.** Recursive key sorting and adapter_id ordering guarantee stable JSON output for identical state and clock inputs.

4. **Observational-only semantics.** Calling `diagnostics()`, `capture_runtime_snapshot()`, `build_runtime_snapshot()`, or `collect_evidence_bundle()` SHALL NOT modify any adapter state, trigger health checks, or perform I/O.

5. **Stable common keys.** The eight common keys listed in § 2 are contractual and SHALL NOT be removed without a version bump.

6. **Adapter-specific keys may grow.** New transport-specific diagnostic keys MAY be added. Existing keys SHALL NOT be removed or have their types changed without a version bump.

## 14. Rendering Evidence

### 14.1 Purpose

Rendering evidence is the structured record that explains why a rendering pass produced its output. It lets operators inspect what happened during rendering: whether fallback was applied, whether content was truncated, and what constraints drove the decision. Rendering evidence is about **rendering decisions**, not about replaying those decisions.

### 14.2 Source: RenderingContext and RenderingResult

Rendering evidence is derived from two frozen dataclasses produced by the rendering pipeline (defined in the Adapter Runtime Specification, § 10):

**RenderingContext** records the input constraints that governed the render call:

| Field               | Evidence role                                                                                                                                                                                                                                                                    |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `delivery_strategy` | Which strategy was selected: `"direct"`, `"fallback_text"`, etc.                                                                                                                                                                                                                 |
| `target_adapter`    | Which adapter the render targets.                                                                                                                                                                                                                                                |
| `target_platform`   | Platform of the target adapter.                                                                                                                                                                                                                                                  |
| `max_text_chars`    | Character budget, or `None` for unlimited.                                                                                                                                                                                                                                       |
| `max_text_bytes`    | UTF-8 byte budget, or `None` for unlimited.                                                                                                                                                                                                                                      |
| `capability_level`  | Capability level for the event's relation type, populated from the `CapabilityDecision` resolved by `CapabilityDecisionResolver`. Reflects the same decision used by Phase 2.5 capability suppression, `FallbackResolver` strategy resolution, and replay BEST_EFFORT filtering. |

**RenderingResult** records the output decisions:

| Field              | Evidence role                                                                                                                   |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| `truncated`        | `True` when the rendered content exceeded a budget and was shortened.                                                           |
| `fallback_applied` | Which fallback strategy was applied: `"strategy_fallback_text"`, `"relation_reply"`, etc., or `None` when no fallback occurred. |
| `payload`          | The rendered content itself. This is the adapter-ready payload, not evidence.                                                   |
| `metadata`         | Additional rendering metadata (format hints, truncation details).                                                               |

Together, these two dataclasses answer the question: "Given these constraints (context), the renderer produced this output (result) with these adjustments (truncated, fallback_applied)."

### 14.3 Payload vs Evidence Distinction

The **payload** is the rendered content intended for adapter delivery. It is the what: the formatted text, the structured message body, the platform-native representation.

**Rendering evidence** is the why: the constraints, decisions, and adjustments that produced that payload. Evidence does not duplicate the payload. It explains the rendering decision.

| Aspect  | Payload                            | Evidence                                                     |
| ------- | ---------------------------------- | ------------------------------------------------------------ |
| Purpose | Content delivered to the transport | Explanation of how and why content was shaped                |
| Carried | `RenderingResult.payload`          | `RenderingContext` fields + `truncated` + `fallback_applied` |
| Size    | Variable, transport-dependent      | Fixed-structure, small                                       |
| Use     | Adapter transports it as-is        | Operator inspects it for debugging and auditing              |

The `RenderingResult.metadata` dict sits between these two: it MAY carry rendering hints (format, truncation byte counts) that are useful for evidence without being the payload itself. Metadata fields are informational and consumers MUST NOT parse them for control-flow decisions.

### 14.4 Durable Receipt Attachment

Rendering evidence becomes durable through attachment to delivery receipts. The `DeliveryReceipt` dataclass carries a `rendering_evidence` field (see Routing and Delivery Specification, § 8.1) that stores a structured record of the rendering evidence for each delivery attempt.

The attachment flow:

1. The rendering pipeline produces a `RenderingResult` with `truncated` and `fallback_applied` fields.
2. The delivery pipeline records the delivery outcome and creates a `DeliveryReceipt`.
3. The `rendering_evidence` field on the receipt stores the rendering evidence, making it durable and queryable.
4. Operators inspecting a receipt chain can determine: was content truncated, was fallback applied, what strategy was used.

The `FallbackApplied` literal vocabulary (`"relation_reply"`, `"relation_reaction"`, `"relation_edit"`, `"relation_delete"`, `"relation_thread"`, `"strategy_fallback_text"`) provides a closed set of fallback reasons.

### 14.5 Replay-Readiness

Rendering evidence is structured to support replay inspection. The frozen, deterministic nature of `RenderingContext` and `RenderingResult` means the same inputs produce the same outputs.

**Current status:** Replay execution **is** implemented as an operator-initiated, in-memory runtime operation (see `medre.core.engine.replay`). Replay re-processes stored canonical events through selected pipeline stages via the `ReplayEngine`. It is **not** a durable job system — there is no automatic crash resume, no replay job queue, and no idempotent delivery guarantee. Replay receipts carry `source="replay"` and `replay_run_id` for audit traceability. `RenderingEvidence` on delivery receipts strengthens post-hoc diagnostics but does not itself replay payloads.

**What replay execution provides:**

- Operator-initiated re-processing of historical canonical events through the pipeline.
- Five behavioural modes: `STRICT`, `RE_RENDER`, `RE_ROUTE`, `BEST_EFFORT`, `DRY_RUN`.
- `BEST_EFFORT` mode delivers to adapters through the normal delivery spine (`PipelineRunner` → `TargetDeliveryService` → `DeliveryLifecycleService`), producing real delivery receipts tagged `source="replay"`.
- Deterministic loop prevention and replay route attribution.
- In-memory execution: no durable replay job queue, no automatic resume after crash.

**What replay execution does _not_ provide (preserved from prior wording):**

- Reconstruction of `RenderingContext` from stored artifacts (re-executing rendering from stored evidence artifacts is not implemented).
- Cross-process or cross-restart evidence replay.
- Idempotent delivery guarantee (replay MAY produce duplicate sends; traceability is not deduplication).

Evidence completeness for post-hoc inspection and deterministic re-rendering given identical context are supported by the frozen nature of the data structures. Replay isolation from live delivery is guaranteed by the `source` and `replay_run_id` tagging on receipts.

### 14.6 Evidence Signals Summary

| Signal              | Source             | Meaning                                                           |
| ------------------- | ------------------ | ----------------------------------------------------------------- |
| `truncated=True`    | `RenderingResult`  | Content was shortened to fit adapter text budgets.                |
| `fallback_applied`  | `RenderingResult`  | A specific fallback strategy was applied. Value identifies which. |
| `delivery_strategy` | `RenderingContext` | The strategy that governed the render call.                       |
| `max_text_bytes`    | `RenderingContext` | The byte budget that may have caused truncation.                  |
| `max_text_chars`    | `RenderingContext` | The character budget that may have caused truncation.             |

An operator inspecting these signals can answer: "Why was this message truncated?" (check `max_text_bytes`/`max_text_chars`), "Why does this message have inline text instead of a native reply?" (check `fallback_applied="relation_reply"`), and "What strategy was active?" (check `delivery_strategy`).

### 14.7 Normative Requirements

1. Renderers MUST set `truncated=True` when content is shortened to fit a budget.
2. Renderers MUST set `fallback_applied` to the appropriate `FallbackApplied` literal when fallback rendering is performed.
3. `fallback_applied` MUST be `None` when no fallback occurred.
4. The `RenderingContext` passed to the renderer MUST accurately reflect the delivery strategy and adapter constraints.
5. Rendering evidence MUST NOT duplicate the payload content.
6. Rendering evidence is observational. It explains decisions; it does not control them.

## 14.8 Capability-Evidence Derivation in Report Dicts

The `delivery_receipt_to_report_dict()` helper in `medre.runtime.reporting` enriches every receipt report dict with capability-evidence fields derived from the receipt's `error` text and/or `rendering_evidence` JSON. No storage schema changes are required; the enrichment is derived at report time from existing receipt fields.

### 14.8.1 Derived Fields

| Field                | Source                                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `suppression_reason` | Parsed from `error` text when `status == "suppressed"` and error matches capability suppression patterns.           |
| `capability_field`   | The `AdapterCapabilities` field that caused suppression (e.g. `reactions`, `replies`). Derived from error text.     |
| `capability_level`   | The three-level decision (`"native"`, `"fallback"`, `"unsupported"`). From `rendering_evidence` JSON or error text. |
| `delivery_strategy`  | The delivery strategy (`"direct"`, `"fallback_text"`, `"skip"`). From `rendering_evidence` JSON or error text.      |

Resolution order:

1. If `rendering_evidence` contains valid JSON with capability fields, those values are used directly.
2. If `status == "suppressed"` and `error` matches known capability suppression patterns, the fields are parsed from the error text.
3. Otherwise, fields are `None`.

### 14.8.2 delivery_state_by_target Enrichment

The incident summary's `delivery_state_by_target` dict groups receipts by composite key `(delivery_plan_id, route_id, target_adapter, target_channel, source, replay_run_id)` and selects the receipt with the highest `attempt_number` per group. The grouping key includes `source` and `replay_run_id` so that live and replay entries for the same target remain distinct. Each target entry now includes the capability-evidence fields from § 14.8.1, plus `source`, `replay_run_id`, `suppression_reason`, and `error`. This gives operators a per-target view of capability suppression without joining back to individual receipts.

| Field                | Present? | Source                          |
| -------------------- | -------- | ------------------------------- |
| `source`             | Yes      | Receipt `source` field          |
| `replay_run_id`      | Yes      | Receipt `replay_run_id` field   |
| `suppression_reason` | Yes      | Derived per § 14.8.1            |
| `capability_field`   | Yes      | Derived per § 14.8.1            |
| `capability_level`   | Yes      | Derived per § 14.8.1            |
| `delivery_strategy`  | Yes      | Derived per § 14.8.1            |
| `error`              | Yes      | Sanitized receipt `error` field |

## 14.9 Rendering Budget Enforcement and Evidence

The LXMF renderer enforces `max_text_chars` (default 16384) from `RenderingContext.max_text_chars`. When the rendered text exceeds the budget, the renderer truncates it and sets `truncated=True` on the `RenderingResult`. The original character length is recorded in `RenderingResult.metadata["original_length"]` for evidence without duplicating the payload.

The `RenderingEvidence` snapshot captures the budget constraints (`max_text_chars`, `max_text_bytes`) and the outcome (`truncated`, `rendered_text_chars`, `rendered_text_bytes`, `original_text_chars`, `original_text_bytes`). Evidence metrics are bounded and payload-free: only character and byte counts are recorded, never the rendered text itself.

> **Known gap.** The rendering budget enforcement is tested at the S-tier level (fake adapters and unit tests). No R-tier evidence exists for budget enforcement against a live LXMF router with real Reticulum transport. The `RE_RENDER` replay mode re-runs rendering but does not currently reconstruct a full capability-aware `RenderingContext` from stored artifacts; it uses whatever context the replay pipeline provides.

## 15. Queued-to-Sent Correlation Evidence

### 15.1 Purpose

Queue-based adapters (e.g., Meshtastic) produce two receipts per delivery: a `queued` receipt at enqueue time and a `sent` receipt when the adapter confirms handoff. Correlating these two receipts requires deterministic matching because multiple deliveries to the same adapter and channel may be in-flight simultaneously.

### 15.2 Deterministic Correlation via delivery_plan_id

The `delivery_plan_id` field provides the correlation key. The pipeline threads `plan.plan_id` through:

1. `RenderingResult.delivery_plan_id` — stamped by `TargetDeliveryService` before adapter delivery.
2. `OutboundNativeRefRecord.delivery_plan_id` — populated by adapter queue processing at send-confirmation time.

For routed live and replay planning, `delivery_plan_id` is deterministic from `event_id`, matched `route_id`, route target index, and a stable JSON target identity. It MUST NOT depend on Python object identity. This lets equivalent live and replay plans correlate to the same semantic target while repeated equivalent targets in one route still receive distinct plan IDs.

When `delivery_plan_id` is present on the outbound ref, `append_queued_to_sent_receipt()` performs an exact match against existing `queued` receipts. This is deterministic regardless of how many overlapping deliveries share the same adapter and channel.

### 15.3 Evidence Signals

| Signal                          | Source                          | Meaning                                                                                        |
| ------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------------- |
| Supplemental `sent` receipt     | `append_queued_to_sent_receipt` | Queued receipt was successfully correlated and finalized                                       |
| No supplemental receipt created | `append_queued_to_sent_receipt` | No matching `queued` receipt found (ordinary no-match logged as debug)                         |
| Ambiguity warning               | `append_queued_to_sent_receipt` | Multiple candidates with cross-plan or cross-channel ambiguity; logged as warning, no receipt  |
| Legacy degraded path applied    | `append_queued_to_sent_receipt` | `delivery_plan_id` was `None`; fallback to adapter match with plan_id and channel uniformity   |
| Same-channel latest-wins        | `append_queued_to_sent_receipt` | Unambiguous retry lineage: same plan_id, same target_channel; latest appended receipt selected |

### 15.4 Normative Requirements

1. When `delivery_plan_id` is available on the outbound ref, the pipeline MUST use exact plan-ID correlation. Legacy fallback MUST only activate when `delivery_plan_id` is `None`.
2. When `delivery_plan_id` is present but no `native_channel_id` is available and multiple plan matches exist, the pipeline MUST check `target_channel` uniformity. If all matches share the same `target_channel` (unambiguous retry lineage), the pipeline MAY select the latest candidate (last appended). If `target_channel` values differ, the pipeline MUST NOT create a supplemental receipt and MUST log a warning.
3. When `delivery_plan_id` is `None` and multiple candidates match by adapter, the pipeline MUST check both `delivery_plan_id` and `target_channel` uniformity. Unambiguous correlation requires exactly one unique `delivery_plan_id` AND exactly one unique `target_channel`. If ambiguous (multiple plans or multiple channels), the pipeline MUST NOT create a supplemental receipt and MUST log a warning.
4. When `delivery_plan_id` is `None` and candidates are unambiguous (exactly one `delivery_plan_id` and exactly one `target_channel`), the pipeline MAY select the latest candidate (last appended).
5. All ambiguous correlation skips MUST log at warning level. Ordinary no-match situations (no candidates at all) MAY remain at debug level. Warning messages MUST include event_id, adapter, delivery_plan_id if available, native_channel_id if available, candidate count, and distinct plan/channel counts where useful.
6. The `delivery_plan_id` on `OutboundNativeRefRecord` is for correlation only. It is not stored in `native_message_refs` storage.
7. Queue acceptance evidence (S-tier) confirms the local node accepted the packet. It does not confirm RF delivery. See § 11 for non-guarantees.

## 16. Evidence Bundle Model

### 16.1 Purpose

The `EvidenceBundle` is a first-class, frozen, read-only model that aggregates all stored evidence for a single event into a deterministic, JSON-safe structure. It is assembled by the `EvidenceCollector` without mutating storage or runtime state.

### 16.2 Contents

| Field               | Type                       | Semantics                                                                                               |
| ------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------- |
| `schema_version`    | `int`                      | Currently `1`. Frozen during pre-release.                                                               |
| `event_id`          | `str`                      | The canonical event ID this bundle covers.                                                              |
| `event_summary`     | `dict or None`             | Summary of the canonical event (kind, source, relation count, payload keys). `None` if event not found. |
| `delivery_receipts` | `tuple[ReceiptSummary, …]` | Ordered by `sequence` (append order). (`to_dict()` produces a JSON array.)                              |
| `native_refs`       | `tuple[dict, …]`           | Ordered by `created_at`, then `id`. (`to_dict()` produces a JSON array.)                                |
| `outbox_items`      | `tuple[dict, …]`           | Ordered by `created_at`, then `outbox_id`. (`to_dict()` produces a JSON array.)                         |
| `replay_run_ids`    | `tuple[str, …]`            | Sorted distinct `replay_run_id` values from receipts. (`to_dict()` produces a JSON array.)              |
| `sources_seen`      | `tuple[str, …]`            | Sorted distinct `source` values from receipts. (`to_dict()` produces a JSON array.)                     |
| `warnings`          | `tuple[str, …]`            | Deterministic insertion-order warnings collected during assembly. (`to_dict()` produces a JSON array.)  |
| `generated_at`      | `str`                      | ISO 8601 timestamp of bundle generation.                                                                |

### 16.3 ReceiptSummary

Each delivery receipt is represented as a `ReceiptSummary` containing receipt ID, sequence, target adapter/channel, status, attempt number, source, replay run ID, failure kind, error, parsed rendering evidence, and created_at timestamp. Full payloads are excluded.

### 16.4 JSON Safety and Deterministic Ordering

1. `to_dict()` returns a plain dict with only JSON-safe types (`dict`, `list`, `str`, `int`, `float`, `bool`, `None`). `json.dumps()` MUST succeed without a custom encoder.
2. `to_json(sort_keys=True)` produces deterministic output for identical inputs.
3. Ordering guarantees:
   - Receipts by `sequence` ascending.
   - Native refs by `created_at ASC, id ASC`.
   - Outbox items by `created_at ASC, outbox_id ASC`.
   - Replay run IDs sorted lexicographically.
   - Warnings in deterministic insertion order.

### 16.5 Invalid Rendering Evidence Handling

`DeliveryReceipt.rendering_evidence` is stored as a string. The collector parses it defensively:

- `None` → `None`, no warning.
- Valid JSON object → parsed `dict`, no warning.
- Valid non-object JSON → `None`, warning with receipt_id/event_id context.
- Invalid JSON → `None`, warning with receipt_id/event_id context and raw length.

The collector MUST NOT crash on invalid JSON.

### 16.6 Replay/Source Aggregation

Replay and source information is aggregated from receipt records:

- `replay_run_ids`: sorted distinct non-`None` `replay_run_id` values across all receipts.
- `sources_seen`: sorted distinct `source` values across all receipts.

### 16.7 Graceful Degradation

- If the event is missing but receipts/native refs/outbox items exist, the bundle is still produced with `event_summary=None` and a warning.
- If no event and no related records exist, the bundle has a warning noting no data was found.
- Missing storage capabilities (e.g. `list_outbox_items_for_event` not implemented) degrade with a warning, not a crash.

### 16.8 Limitations

The evidence bundle is:

- **Not a tracing backend.** It is a point-in-time read-only snapshot.
- **Not a replay job system.** It does not queue or execute replays.
- **Not a crash recovery mechanism.** It reads from current storage state only.
- **Not an idempotency guarantee.** Multiple collections at different times may produce different bundles if storage state changed between calls.
- **Read-only.** Collection MUST NOT mutate storage or runtime state.

## 17. Operator Diagnostics Traceability

An operator inspecting an :class:`EvidenceBundle` or a report dict from :func:`delivery_receipt_to_report_dict` can answer the following traceability questions from evidence alone, without consulting logs or source code:

| Question                                  | Evidence source                                                                | Key fields                                                                                     |
| ----------------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Was this event processed?                 | `event_summary` in :class:`EvidenceBundle`                                     | `event_id`, `event_kind`, `source_adapter`                                                     |
| Which route matched?                      | `delivery_state_by_target` entry or receipt                                    | `route_id`                                                                                     |
| Which target was selected?                | `delivery_state_by_target` composite key                                       | `target_adapter`, `target_channel`, `target_identity` (via `delivery_plan_id`)                 |
| What plan ID was assigned?                | `delivery_state_by_target` entry or receipt                                    | `delivery_plan_id` (deterministic via :func:`stable_delivery_plan_id`)                         |
| What strategy was chosen?                 | `rendering_evidence` JSON on receipt, or `delivery_state_by_target` enrichment | `delivery_strategy` (`"direct"`, `"fallback_text"`, `"skip"`)                                  |
| What capability field drove the decision? | `delivery_state_by_target` enrichment or parsed from `error`                   | `capability_field` (e.g. `reactions`, `replies`, `text`) or `None` for loop/policy suppression |
| What is the delivery status?              | Receipt                                                                        | `status` (`"sent"`, `"queued"`, `"suppressed"`, `"failed"`, `"dead_lettered"`)                 |
| Why did delivery fail?                    | Receipt and enrichment                                                         | `failure_kind`, `failure_kind_detail`, `error`, `suppression_reason`                           |
| Was this a live delivery or replay?       | Receipt                                                                        | `source` (`"live"` or `"replay"`), `replay_run_id`                                             |
| How many retry attempts occurred?         | Receipt chain                                                                  | `attempt_number`, `parent_receipt_id` (links in chain), `next_retry_at` (`None` for exhausted) |

### 17.1 Evidence Completeness Per Pipeline Stage

The evidence bundle covers all five pipeline stages for a single event:

| Pipeline stage | Evidence captured                                  | Key fields in bundle                                                        |
| -------------- | -------------------------------------------------- | --------------------------------------------------------------------------- |
| Store          | Event persisted in storage                         | `event_summary` with `event_id`, `event_kind`, `source_adapter`             |
| Route          | Route matched and route ID assigned                | `route_id` on receipt, in `delivery_state_by_target`                        |
| Plan           | Delivery plan constructed with deterministic ID    | `delivery_plan_id` on receipt, in `delivery_state_by_target`                |
| Render         | Rendering strategy and capability level captured   | `rendering_evidence` JSON with `delivery_strategy`, `capability_level`      |
| Deliver        | Delivery outcome status and failure classification | `status`, `failure_kind`, `error` on receipt, in `delivery_state_by_target` |

A single evidence bundle for a fully-processed event contains data from all five stages simultaneously.

### 17.2 Report Dict Enrichment

:func:`delivery_receipt_to_report_dict` enriches every receipt report dict with the following derived fields. No storage schema changes are required; enrichment is derived at report time from existing receipt fields:

| Field                 | Source                                                                                                                 |
| --------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `suppression_reason`  | Parsed from `error` text when `status == "suppressed"` and error matches capability or loop suppression patterns.      |
| `capability_field`    | The :class:`AdapterCapabilities` field that caused suppression (e.g. `reactions`, `replies`). Derived from error text. |
| `capability_level`    | The three-level decision (`"native"`, `"fallback"`, `"unsupported"`). From `rendering_evidence` JSON or error text.    |
| `delivery_strategy`   | The delivery strategy (`"direct"`, `"fallback_text"`, `"skip"`). From `rendering_evidence` JSON or error text.         |
| `failure_kind_detail` | More specific classification derived from error patterns (e.g. `"e2ee_blocked"`, `"meshtastic_queue_rejected"`).       |
| `retryable`           | Derived from `status`, `failure_kind`, and `next_retry_at`. `True` only for transient failures or scheduled retries.   |

### 17.3 delivery_state_by_target Enrichment

The incident summary's `delivery_state_by_target` dict groups receipts by composite key `(delivery_plan_id, route_id, target_adapter, target_channel, source, replay_run_id)` and selects the receipt with the highest `attempt_number` per group. Including `source` and `replay_run_id` in the key keeps live and replay entries distinct. Each target entry includes:

| Field                 | Source                                |
| --------------------- | ------------------------------------- |
| `source`              | Receipt `source` field                |
| `replay_run_id`       | Receipt `replay_run_id` field         |
| `suppression_reason`  | Derived per § 17.2                    |
| `capability_field`    | Derived per § 17.2                    |
| `capability_level`    | Derived per § 17.2                    |
| `delivery_strategy`   | Derived per § 17.2                    |
| `error`               | Sanitised receipt `error` field       |
| `failure_kind`        | Receipt `failure_kind` field          |
| `failure_kind_detail` | Derived per § 17.2                    |
| `retryable`           | Derived per § 17.2                    |
| `next_retry_at`       | Receipt `next_retry_at` field         |
| `attempt_number`      | Highest `attempt_number` in the group |

When both live and replay receipts exist for the same event, the bundle contains separate `delivery_state_by_target` entries with distinct `source` values (`"live"` and `"replay"`), allowing the operator to distinguish live from replay delivery for the same target.
