# Evidence Parity Audit

> **Classification:** Developer reference (derived from [diagnostics-evidence.md](../spec/diagnostics-evidence.md))
> **Audience:** Runtime developers, adapter authors, code reviewers.
> **Authority:** [diagnostics-evidence.md](../spec/diagnostics-evidence.md) is the normative specification. This document records adapter-level evidence parity findings, gaps, and a prioritized implementation list. If this document conflicts with the spec, the spec is correct.
> **Scope:** Operational runtime evidence produced by the four transport adapters (Matrix, Meshtastic, MeshCore, LXMF). Does not cover lifecycle, capability, SDK parity, or boundary documentation owned by other workers.
> **Branch:** `main` post-merge of `adapter-sdk-parity`.

## 1. Summary

This audit compares how the four MEDRE adapters produce evidence through their `diagnostics()` methods and `health_check()` APIs. The [Diagnostics and Evidence Specification](../spec/diagnostics-evidence.md) §2 defines eight contractual common keys that SHALL appear in every adapter's `diagnostics()` output. This audit measures actual production against that contract, identifies gaps, inconsistencies, and misleading evidence, and ranks implementation opportunities by operational value.

**Key finding:** No adapter includes a `health` key in its `diagnostics()` output. Two adapters (Matrix, Meshtastic) omit `mode` from their real-adapter `diagnostics()`. Three adapters (Meshtastic, MeshCore, LXMF) produce incomplete common-key coverage when no session exists. The normalization layer (`diagnostic_contract.py`) resolves missing keys to `None`, preserving JSON-safety but losing information that operators need.

## 2. Relevant Testing Rules

The following rules from [testing.md](testing.md) govern how evidence-related tests must be written and maintained:

- **File size limits:** Test files stay below 1,500 lines (hard cap). Target below 1,200 lines.
- **Test tiers:** Evidence tests are typically tier 1 (`fake_pipeline`) or tier 2 (`fake_adapter_callback`). Never overclaim evidence level. Tests using fake adapters must be labeled `fake_pipeline`, not "docker" or "live".
- **Honest evidence reporting:** The `medre smoke --json` report uses `evidence_level: fake_bridge` intentionally. It does not overclaim.
- **No fixed sleeps:** Use `wait_until()` or deterministic hooks. Never `asyncio.sleep()` in tests.
- **Async mocking:** Match mock type to production call shape. `await` calls use `AsyncMock`; attribute access uses plain attributes or `PropertyMock`.
- **Warnings are bugs:** `ResourceWarning` and `RuntimeWarning` about unawaited coroutines indicate real issues.
- **No compatibility shims in tests:** Test and production code paths are identical.
- **Test execution discipline:** No timeout wrappers for routine runs, no output truncation, no broad suite after scoped validation passes, stop after first hang.

## 3. Evidence Categories Audited

| Category                | Source                                                          | Description                                                        |
| ----------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------ |
| Diagnostics             | `adapter.diagnostics()`                                         | Common and per-adapter diagnostic key shapes                       |
| Queue evidence          | Queue subsystem                                                 | Outbound queue depth, send counts, failure counts                  |
| Retry evidence          | Session + `RetryWorker`                                         | Reconnect attempts, retry outcomes                                 |
| Shutdown evidence       | `ShutdownEvidence` + receipts                                   | Drain state, pending outbox, shutdown rejection receipts           |
| Reconnect evidence      | Session diagnostics                                             | `reconnecting`, `reconnect_attempts`, reconnect loop state         |
| Ingress evidence        | Classifier counters + inbound counters                          | Packets seen/relayed/ignored/dropped, inbound published/suppressed |
| Adapter health evidence | `health_check()` → `AdapterInfo` → `normalize_adapter_health()` | Health vocabulary, fake/live detection, lifecycle override         |

## 4. Contractual Common-Key Parity Table

The spec (§2) requires eight keys in every adapter's `diagnostics()` output. The table below shows the actual state for each adapter. Meshtastic is used as the maturity reference where noted because it has the most complete queue evidence surface.

### 4.1 Per-Adapter Common-Key Coverage

#### Matrix

| Key                           | Expected type | Value source                                  | Actual type   | Status             | Notes                                                      |
| ----------------------------- | ------------- | --------------------------------------------- | ------------- | ------------------ | ---------------------------------------------------------- |
| `connected`                   | `bool`        | `diag.connected` (session dataclass)          | `bool`        | **present**        | Top-level when session exists; `False` in fallback         |
| `health`                      | `str`         | Not produced                                  | N/A           | **missing**        | Available only via `health_check()` → `AdapterInfo.health` |
| `mode`                        | `str`         | Not produced in real adapter                  | N/A           | **missing (real)** | Fake adapter emits `"mode": "fake"`; real adapter omits    |
| `reconnecting`                | `bool`        | `diag.reconnecting` (session dataclass)       | `bool`        | **present**        | Sync recovery track                                        |
| `reconnect_attempts`          | `int`         | `diag.reconnect_attempts` (session dataclass) | `int`         | **present**        |                                                            |
| `last_error`                  | `str or None` | `diag.last_sync_error`                        | `str or None` | **renamed**        | Named `last_sync_error` in output; spec acknowledges this  |
| `transient_delivery_failures` | `int`         | `self._transient_delivery_failures`           | `int`         | **present**        | Adapter-level counter                                      |
| `permanent_delivery_failures` | `int`         | `self._permanent_delivery_failures`           | `int`         | **present**        | Adapter-level counter                                      |

**Matrix assessment:** 5/8 keys fully conformant at top level. `health` absent from diagnostics (available through separate API). `mode` absent from real adapter. `last_error` has Matrix-specific name.

#### Meshtastic

| Key                           | Expected type | Value source                               | Actual type   | Status               | Notes                                                               |
| ----------------------------- | ------------- | ------------------------------------------ | ------------- | -------------------- | ------------------------------------------------------------------- |
| `connected`                   | `bool`        | `session_diag.connected`                   | `bool`        | **present (nested)** | In `session` sub-dict only; absent when session is None             |
| `health`                      | `str`         | Not produced                               | N/A           | **missing**          | Available only via `health_check()` → `AdapterInfo.health`          |
| `mode`                        | `str`         | Not produced in real adapter               | N/A           | **missing (real)**   | Has `connection_type` at adapter level; fake emits `"mode": "fake"` |
| `reconnecting`                | `bool`        | `session_diag.reconnecting`                | `bool`        | **present (nested)** | In `session` sub-dict                                               |
| `reconnect_attempts`          | `int`         | `session_diag.reconnect_attempts`          | `int`         | **present (nested)** | In `session` sub-dict                                               |
| `last_error`                  | `str or None` | `session_diag.last_error`                  | `str or None` | **present (nested)** | In `session` sub-dict                                               |
| `transient_delivery_failures` | `int`         | `session_diag.transient_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                               |
| `permanent_delivery_failures` | `int`         | `session_diag.permanent_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                               |

**Meshtastic assessment:** 7/8 keys present when session exists (all nested in session sub-dict). `health` absent. `mode` absent from real adapter (has `connection_type` instead). **Critical gap:** when `self._session is None`, the entire `session` sub-dict is omitted, removing all seven nested common keys from the output. The adapter-level dict contains only `adapter_id`, `platform`, `started`, `connection_type`, queue stats, classifier counters, and inbound counters — none of the eight contractual keys.

**Meshtastic as maturity reference:** Meshtastic has the richest queue evidence surface (14 queue-related keys: `queue_pending`, `queue_total_sent`, `queue_total_failed`, `queue_total_enqueued`, `queue_total_dequeued`, `queue_total_rejected`, `queue_total_requeued`, `queue_total_exhausted`, `queue_total_permanent_failed`, `queue_max_size`, `queue_send_max_attempts`, `queue_utilization_pct`, `queue_delay_between_messages`, `queue_last_send_time`). It also has the most complete classifier counter surface (12 counters) and startup backlog evidence.

#### MeshCore

| Key                           | Expected type | Value source                                            | Actual type   | Status                   | Notes                                                                         |
| ----------------------------- | ------------- | ------------------------------------------------------- | ------------- | ------------------------ | ----------------------------------------------------------------------------- |
| `connected`                   | `bool`        | `session.connected` (via `sanitize_diagnostic_mapping`) | `bool`        | **present (nested)**     | In `session` sub-dict; absent when session is None                            |
| `health`                      | `str`         | Not produced                                            | N/A           | **missing (documented)** | Spec §2 explicitly notes MeshCore exception: health via `health_check()` only |
| `mode`                        | `str`         | `self._config.connection_type`                          | `str`         | **present**              | At adapter top level AND in session sub-dict                                  |
| `reconnecting`                | `bool`        | `session.reconnecting`                                  | `bool`        | **present (nested)**     | In `session` sub-dict                                                         |
| `reconnect_attempts`          | `int`         | `session.reconnect_attempts`                            | `int`         | **present (nested)**     | In `session` sub-dict                                                         |
| `last_error`                  | `str or None` | `session.last_error`                                    | `str or None` | **present (nested)**     | In `session` sub-dict                                                         |
| `transient_delivery_failures` | `int`         | `session.transient_delivery_failures`                   | `int`         | **present (nested)**     | In `session` sub-dict                                                         |
| `permanent_delivery_failures` | `int`         | `session.permanent_delivery_failures`                   | `int`         | **present (nested)**     | In `session` sub-dict                                                         |

**MeshCore assessment:** 7/8 keys present when session exists (all nested in session sub-dict, except `mode` at top level). `health` explicitly documented as not produced. Same no-session gap as Meshtastic: when `self._session is None`, the `session` sub-dict is omitted entirely, and the adapter-level dict contains only `adapter_id`, `platform`, `started`, `mode`, classifier counters, and `inbound_published`.

#### LXMF

| Key                           | Expected type | Value source                                | Actual type   | Status               | Notes                                                      |
| ----------------------------- | ------------- | ------------------------------------------- | ------------- | -------------------- | ---------------------------------------------------------- |
| `connected`                   | `bool`        | `self._session.connected`                   | `bool`        | **present (nested)** | In `session` sub-dict; absent when session is None         |
| `health`                      | `str`         | Not produced                                | N/A           | **missing**          | Available only via `health_check()` → `AdapterInfo.health` |
| `mode`                        | `str`         | `self._config.connection_type`              | `str`         | **present**          | At adapter top level AND in session sub-dict               |
| `reconnecting`                | `bool`        | `self._session.reconnecting`                | `bool`        | **present (nested)** | In `session` sub-dict                                      |
| `reconnect_attempts`          | `int`         | `self._session.reconnect_attempts`          | `int`         | **present (nested)** | In `session` sub-dict                                      |
| `last_error`                  | `str or None` | `self._session.last_error`                  | `str or None` | **present (nested)** | In `session` sub-dict                                      |
| `transient_delivery_failures` | `int`         | `self._session.transient_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                      |
| `permanent_delivery_failures` | `int`         | `self._session.permanent_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                      |

**LXMF assessment:** 7/8 keys present when session exists. `health` absent. Same no-session gap.

**LXMF spec/implementation discrepancy:** The spec (§3.4 LXMF note) states: "Session diagnostics are exposed directly via the `LxmfSessionDiagnostics` frozen dataclass. The LXMF adapter does not layer its own outer diagnostics dict on top." However, the actual `LxmfAdapter.diagnostics()` implementation layers an outer dict with `adapter_id`, `platform`, `started`, `mode`, and a `session` sub-dict. Additionally, `LxmfSessionDiagnostics` contains fields (`last_message_time`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`) that are defined in the spec (§3.4) as top-level keys but are not surfaced in the adapter's diagnostics output at all. The adapter only exposes `connected`, `router_running`, `reconnecting`, `reconnect_attempts`, `transient_delivery_failures`, `permanent_delivery_failures`, `last_error`, and `mode` in its session sub-dict.

## 5. Evidence Category Parity by Adapter

### 5.1 Diagnostics Evidence

| Aspect                          | Matrix                 | Meshtastic                      | MeshCore                                    | LXMF                            |
| ------------------------------- | ---------------------- | ------------------------------- | ------------------------------------------- | ------------------------------- |
| Common keys at top level        | 5 (no health, no mode) | 0 (all nested)                  | 1 (`mode` only)                             | 1 (`mode` only)                 |
| Common keys in session sub-dict | N/A (flat)             | 7 (no health)                   | 7 (no health)                               | 7 (no health)                   |
| Session-less fallback           | Full fallback dict     | No common keys                  | No common keys                              | No common keys                  |
| Transport-specific keys         | 21+                    | 30+                             | 10+                                         | 3                               |
| Diagnostics shape               | Flat dict              | Adapter dict + session sub-dict | Adapter dict + session sub-dict (sanitized) | Adapter dict + session sub-dict |

### 5.2 Queue Evidence

| Aspect       | Matrix | Meshtastic                                     | MeshCore | LXMF                               |
| ------------ | ------ | ---------------------------------------------- | -------- | ---------------------------------- |
| Queue depth  | None   | `queue_pending`                                | None     | `pending_delivery_count` (limited) |
| Send counts  | None   | `queue_total_sent`, `queue_total_failed`, etc. | None     | None                               |
| Queue health | None   | `queue` dict (utilization, timing)             | None     | None                               |
| Parity       | None   | **Reference implementation**                   | None     | Minimal                            |

**Gap:** MeshCore and Matrix produce zero queue evidence. Operators running MeshCore or Matrix adapters have no visibility into outbound queue depth, send throughput, or queue health. LXMF has minimal queue evidence (`pending_delivery_count` only via session diagnostics dataclass, not surfaced in adapter diagnostics).

### 5.3 Retry Evidence

| Aspect               | Matrix                 | Meshtastic                | MeshCore                  | LXMF                   |
| -------------------- | ---------------------- | ------------------------- | ------------------------- | ---------------------- |
| `reconnect_attempts` | Present                | Present                   | Present                   | Present                |
| `reconnecting` flag  | Present                | Present                   | Present                   | Present                |
| Reconnect backoff    | Session-level          | Bounded (max 10 attempts) | Bounded (max 10 attempts) | Session-level          |
| Retry worker events  | Runtime-level (shared) | Runtime-level (shared)    | Runtime-level (shared)    | Runtime-level (shared) |

**Assessment:** Retry evidence is the most uniform category. All four adapters expose `reconnecting` and `reconnect_attempts`. Meshtastic and MeshCore share a bounded reconnect loop pattern (max 10 attempts with jitter). The retry worker event surface (`RuntimeEventType` — 8 retry-related event types) is runtime-level and shared across all adapters.

### 5.4 Shutdown Evidence

Shutdown evidence is runtime-level (`ShutdownEvidence` dataclass) and not adapter-specific. All adapters share:

- `shutdown_status`, `resume_expected`, `outbox_shutdown_policy`
- `pending_outbox_counts`, `drain_timeout_detected`
- `tasks_cancelled`, `evidence_flush_status`
- Durable `shutdown_rejection` delivery receipts in storage

**Assessment:** Fully uniform. No adapter-specific gaps.

### 5.5 Reconnect Evidence

| Aspect               | Matrix            | Meshtastic                    | MeshCore                      | LXMF              |
| -------------------- | ----------------- | ----------------------------- | ----------------------------- | ----------------- |
| `reconnecting`       | Session dataclass | Session dataclass             | Session mutable dataclass     | Session dataclass |
| `reconnect_attempts` | Session dataclass | Session dataclass             | Session mutable dataclass     | Session dataclass |
| Max attempts         | Session-defined   | 10 (constant)                 | 10 (constant)                 | Session-defined   |
| Backoff config       | Session-level     | Base 1s, max 30s, ±25% jitter | Base 1s, max 30s, ±25% jitter | Session-level     |

**Assessment:** Reconnect evidence is structurally uniform. Meshtastic and MeshCore share identical backoff parameters and max-attempt bounds. Matrix and LXMF have session-level reconnect semantics.

### 5.6 Ingress Evidence

| Aspect               | Matrix                 | Meshtastic                   | MeshCore    | LXMF    |
| -------------------- | ---------------------- | ---------------------------- | ----------- | ------- |
| Classifier counters  | N/A (not packet-based) | 12 counters                  | 10 counters | N/A     |
| `inbound_published`  | Present                | Present                      | Present     | Present |
| Suppression counters | 4 types                | 1 (startup backlog)          | None        | None    |
| Startup backlog      | None                   | `startup_backlog_*` (3 keys) | None        | None    |

**Gap:** MeshCore has classifier counters but no suppression breakdown. LXMF has only `inbound_published` with no classifier or suppression counters. This is expected given the different transport models (LXMF is not packet-classification-based in the same way).

### 5.7 Adapter Health Evidence

All adapters produce health through the same path:

1. `health_check()` returns `AdapterInfo` with a `health` string
2. `normalize_adapter_health()` projects to a normalized dict with `health`, `fake_or_live`, `capabilities`, `details`
3. Lifecycle state overrides apply (`INITIALIZING` → `starting`, `STOPPING` → `stopping`)
4. The six health vocabulary strings are enforced: `healthy`, `degraded`, `failed`, `unknown`, `starting`, `stopping`

**Assessment:** Health evidence is structurally uniform across adapters. The gap is that this evidence is not included in `diagnostics()` output — it requires a separate `health_check()` call.

## 6. Identified Gaps, Inconsistencies, and Misleading Evidence

### 6.1 `health` missing from all adapters' `diagnostics()` output

**Severity:** High
**Scope:** All four adapters
**Description:** The spec (§2) requires eight common keys in `diagnostics()` output, including `health`. No adapter includes a `health` key in its `diagnostics()` return value. Health is available only through the separate `health_check()` API. The normalization layer (`diagnostic_contract.py`) resolves `health` to `None`.

**Operator impact:** Operators examining raw `diagnostics()` output see `health: null` for all adapters. This is misleading — the adapter may be healthy, but the diagnostics surface doesn't show it. Operators must call `health_check()` separately or rely on the `normalize_adapter_health()` projection.

**Constraint note:** Adapters report facts only. Including `health` in `diagnostics()` would require adapters to duplicate the `AdapterInfo.health` field or derive it at diagnostics time. This is feasible because `health_check()` is observational and the health value is already computed.

### 6.2 No-session fallback incompleteness (Meshtastic, MeshCore, LXMF)

**Severity:** High
**Scope:** Meshtastic, MeshCore, LXMF
**Description:** When `self._session is None` (pre-start, post-failure, post-stop), these three adapters omit the `session` sub-dict entirely. This removes all seven nested common keys (`connected`, `reconnecting`, `reconnect_attempts`, `last_error`, `transient_delivery_failures`, `permanent_delivery_failures`) from the output. Only `adapter_id`, `platform`, `started`, `mode`, and counters remain.

**Maturity reference (Matrix):** The Matrix adapter handles this correctly with an explicit fallback dict that includes `connected: False`, `reconnecting: False`, `reconnect_attempts: 0`, and all other common keys with safe defaults.

**Operator impact:** When an adapter fails to start, its diagnostics output lacks the very keys operators need most (`connected`, `last_error`). The operator sees adapter-level metadata but no transport state.

**Risk:** `normalize_diagnostics()` resolves all missing common keys to `None`. This is safe (no invented success) but loses the explicit `False`/`0` signal that a fallback dict would provide.

### 6.3 `mode` missing from real adapter diagnostics (Matrix, Meshtastic)

**Severity:** Medium
**Scope:** Matrix (real adapter), Meshtastic (real adapter)
**Description:** The Matrix real adapter's `diagnostics()` does not include a `mode` key. The Meshtastic real adapter uses `connection_type` at the adapter level instead of `mode`. Both fake adapters emit `"mode": "fake"`.

**Operator impact:** The `normalize_diagnostics()` layer resolves `mode` to `None` for real Matrix and Meshtastic adapters. Operators cannot determine transport mode from raw diagnostics.

**Constraint note:** The adapter knows its mode from `self._config.connection_type`. Including a `mode` key is a simple addition that reports an existing fact.

### 6.4 `last_error` naming inconsistency (Matrix)

**Severity:** Low
**Scope:** Matrix only
**Description:** The spec (§2 Matrix note) acknowledges that Matrix uses `last_sync_error` instead of `last_error`. The normalization layer looks for `last_error` and will not find the Matrix-specific name.

**Operator impact:** Tooling that checks `diagnostics["last_error"]` will see `None` for Matrix adapters even when a sync error exists. Operators must check `last_sync_error` specifically for Matrix.

**Status:** Acknowledged by spec. Not a bug, but an inconsistency that reduces parity.

### 6.5 Common-key nesting inconsistency

**Severity:** Low
**Scope:** All adapters
**Description:** Matrix exposes common keys at the adapter dict's top level. Meshtastic, MeshCore, and LXMF nest common keys in a `session` sub-dict. The spec (§2) allows both: "MAY appear directly or nested in a session sub-dict."

**Operator impact:** Tooling must handle both flat and nested layouts. The `normalize_diagnostics()` function handles this by extracting common keys from the flat dict only. Keys in the `session` sub-dict are treated as transport-specific, not common keys.

**Status:** Permitted by spec. The normalization layer handles it.

### 6.6 LXMF spec/implementation discrepancy

**Severity:** Medium
**Scope:** LXMF only
**Description:** The spec (§3.4 LXMF note) states that the LXMF adapter does not layer its own outer diagnostics dict. The actual implementation does layer an outer dict (`adapter_id`, `platform`, `started`, `mode`, `session`). Additionally, `LxmfSessionDiagnostics` defines fields (`last_message_time`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`) that the spec lists as top-level LXMF diagnostic keys but the adapter's `diagnostics()` does not surface them.

**Operator impact:** The four spec-defined keys (`last_message_time`, `known_path_count`, `propagation_enabled`, `pending_delivery_count`) are not visible through the adapter's diagnostics. Operators using the spec as reference will not find these values.

**Risk:** The session diagnostics dataclass has these fields, but the adapter's `diagnostics()` method cherry-picks which fields to include in the session sub-dict and omits them.

### 6.7 Misleading evidence: `connected: true` does not guarantee next-operation success

**Severity:** Informational
**Scope:** All adapters
**Description:** This is an observational caveat, not a bug. The spec (§12) explicitly states that `connected: true` is a point-in-time snapshot. All diagnostics are observational and do not guarantee future operation success. This is correctly documented.

## 7. Prioritized Implementation List

Ranked by operational value — the value each fix provides to operators diagnosing issues in production or pre-production scenarios. Meshtastic is used as the maturity reference where noted.

### Priority 1 (P0): No-session fallback completeness

**Adapters:** Meshtastic, MeshCore, LXMF
**Fix:** Add explicit fallback values for all eight common keys when `self._session is None`, matching the Matrix adapter's pattern.
**Operational value:** Highest. When an adapter fails to start or crashes, operators need to see `connected: false`, `reconnecting: false`, `reconnect_attempts: 0`, etc. Without this, pre-start and post-failure diagnostics are nearly empty for Meshtastic, MeshCore, and LXMF. This is the single highest-impact evidence gap because it affects the scenarios where diagnostics matter most.
**Implementation pattern (from Matrix):**

```
if session is None:
    return {
        "connected": False,
        "reconnecting": False,
        "reconnect_attempts": 0,
        "last_error": None,
        "transient_delivery_failures": 0,
        "permanent_delivery_failures": 0,
        ...adapter-level keys...,
    }
```

**Estimated effort:** Low. Each adapter needs ~15 lines of fallback dict.

### Priority 2 (P0): `health` key in diagnostics output

**Adapters:** All four
**Fix:** Include the current health string (from `AdapterInfo.health` or a cached health value) in the `diagnostics()` output under the `health` key.
**Operational value:** High. The spec mandates `health` as one of eight contractual keys. Operators and tooling expect it. Currently, `normalize_diagnostics()` resolves it to `None`, losing information that exists in the adapter. This is the most visible spec compliance gap.
**Constraint:** Adapters report facts only. The `health` value is already a fact computed by the adapter. Including it in `diagnostics()` does not require new health polling or state changes.
**Design choice:** Adapters could cache the last `AdapterInfo.health` value and include it in `diagnostics()`. Alternatively, the normalization layer could merge health from `health_check()` results when available.
**Estimated effort:** Low to medium. Requires coordinated change across all four adapters plus fakes.

### Priority 3 (P1): `mode` key in real adapter diagnostics

**Adapters:** Matrix, Meshtastic
**Fix:** Include `"mode": self._config.connection_type` (or equivalent) in the real adapter's `diagnostics()` output, matching the fake adapter behavior and MeshCore/LXMF pattern.
**Operational value:** Medium. Operators need to know whether an adapter is in `fake`, `tcp`, `serial`, or `ble` mode from diagnostics output. Currently, real Matrix and Meshtastic adapters don't expose this. Meshtastic has `connection_type` at adapter level which is semantically equivalent but uses a non-standard key name.
**Estimated effort:** Trivial. One line per adapter.

### Priority 4 (P1): `last_error` normalization for Matrix

**Adapters:** Matrix
**Fix:** Either include both `last_error` and `last_sync_error` in Matrix diagnostics, or update the normalization layer to recognize `last_sync_error` as a Matrix-specific alias for `last_error`.
**Operational value:** Medium. Cross-adapter tooling that checks `last_error` currently sees `None` for Matrix even when a sync error exists. This creates a blind spot in error diagnostics for the most operationally critical adapter.
**Estimated effort:** Low. Either a one-line addition in Matrix adapter diagnostics or a mapping entry in the normalization layer.

### Priority 5 (P2): LXMF adapter diagnostics completeness

**Adapters:** LXMF
**Fix:** Surface `last_message_time`, `known_path_count`, `propagation_enabled`, and `pending_delivery_count` from `LxmfSessionDiagnostics` in the adapter's `diagnostics()` session sub-dict or at top level, matching the spec's §3.4 key listing.
**Operational value:** Medium. These four keys are spec-defined but not exposed. `last_message_time` indicates when the adapter last received traffic. `known_path_count` and `propagation_enabled` indicate Reticulum network state. `pending_delivery_count` shows outbound delivery pressure.
**Estimated effort:** Low. Four additional lines in `LxmfAdapter.diagnostics()`.

### Priority 6 (P2): Queue evidence for MeshCore and Matrix

**Adapters:** MeshCore, Matrix
**Fix:** Add basic queue evidence (`queue_pending`, `queue_total_sent`, `queue_total_failed`) to adapter diagnostics, using Meshtastic as the reference pattern.
**Operational value:** Medium. Meshtastic's queue evidence is the most operationally valuable evidence surface for understanding outbound delivery pressure. MeshCore and Matrix have no queue visibility. Operators running these adapters cannot diagnose send backpressure or queue stalls.
**Constraint:** Matrix uses nio's sync loop rather than a discrete queue. Queue evidence for Matrix would need to track in-flight delivery promises differently than Meshtastic's queue system. MeshCore may not have a discrete queue either.
**Estimated effort:** Medium. Requires defining what "queue" means for each transport.

### Priority 7 (P2): Ingress evidence parity for LXMF

**Adapters:** LXMF
**Fix:** Add ingress counters beyond `inbound_published`: at minimum, a `packets_ignored` or `messages_suppressed` counter.
**Operational value:** Low to medium. LXMF's ingress model is simpler than Meshtastic/MeshCore (no packet classification in the same sense), but some inbound filtering evidence would help operators understand whether messages are being received but not published.
**Estimated effort:** Low.

### Priority 8 (P3): Common-key location normalization

**Adapters:** All four
**Fix:** Either hoist session sub-dict common keys to the adapter's top level, or document that the normalization layer handles extraction and operators should use `normalize_diagnostics()` consistently.
**Operational value:** Low. The normalization layer already handles this. Direct consumers of adapter `diagnostics()` see inconsistency, but the `RuntimeSnapshot` and evidence bundle normalize the view.
**Estimated effort:** Low to medium, depending on approach.

## 8. Constraints Observed

This audit observes the following constraints, which any implementation work must respect:

1. **Adapters report facts only.** No adapter inventories success, infers state, or performs health polling through diagnostics.
2. **Runtime evidence must match actual guarantees.** The observational caveat (spec §12) applies: diagnostics are point-in-time snapshots, not authoritative state.
3. **Capability/evidence must not overclaim behavior.** Evidence does not prove reliability, ordering, latency bounds, or production readiness.
4. **No schema churn.** This audit recommends filling gaps, not changing existing key shapes or types.
5. **No compatibility layers.** Implementation fixes should be direct, not shimmed.
6. **No new evidence schemas.** The eight contractual keys are stable. New keys go into transport-specific sections.

## 9. Sources Examined

| Source                                              | Purpose                                                                                       |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| `docs/spec/diagnostics-evidence.md`                 | Normative contract for 8 common keys, evidence classification, observational caveat           |
| `src/medre/core/evidence/adapter_status.py`         | `AdapterStatusEvidence` dataclass, operator status derivation                                 |
| `src/medre/core/supervision/diagnostic_contract.py` | `normalize_diagnostics()`, `COMMON_DIAGNOSTIC_KEYS`, sanitization                             |
| `docs/dev/runtime-evidence-completeness-audit.md`   | Runtime evidence surface inventory, event taxonomy                                            |
| `docs/dev/testing.md`                               | Testing rules, file size limits, evidence honesty                                             |
| `tests/test_adapter_health.py`                      | Health normalization tests, vocabulary coverage                                               |
| `tests/test_adapter_status_evidence.py`             | Operator status derivation, lifecycle state mapping                                           |
| `src/medre/adapters/matrix/adapter.py`              | Matrix diagnostics implementation (lines 806–896)                                             |
| `src/medre/adapters/matrix/session.py`              | `MatrixSessionDiagnostics` dataclass (lines 105–139)                                          |
| `src/medre/adapters/meshtastic/adapter.py`          | Meshtastic diagnostics implementation (lines 750–825)                                         |
| `src/medre/adapters/meshtastic/session.py`          | `MeshtasticSessionDiagnostics` dataclass (lines 70–85)                                        |
| `src/medre/adapters/meshcore/adapter.py`            | MeshCore diagnostics implementation (lines 559–584)                                           |
| `src/medre/adapters/meshcore/session.py`            | MeshCore `_SessionDiagnostics` dataclass (lines 120–133), session diagnostics (lines 422–443) |
| `src/medre/adapters/lxmf/adapter.py`                | LXMF diagnostics implementation (lines 229–259)                                               |
| `src/medre/adapters/lxmf/session.py`                | `LxmfSessionDiagnostics` dataclass (lines 326–344)                                            |

## 10. Validation Surfaces

The following test areas guard the evidence contracts audited here:

- `test_adapter_health.py`: Health vocabulary coverage, normalization structure, lifecycle state override, fake/live detection
- `test_adapter_status_evidence.py`: Operator status derivation, input tolerance, serialization, lifecycle state mapping
- `test_runtime_snapshot.py`: Snapshot construction, adapter ordering, JSON-safety, key sorting
- `test_evidence_bundle.py`: Bundle shape, section statuses, hoisted fields, schema version
- `test_diagnostic_contract.py`: Common key extraction, sanitization, missing key fallback

Any implementation work stemming from this audit should extend these test surfaces rather than creating new ones, respecting the file size limits in testing.md.
