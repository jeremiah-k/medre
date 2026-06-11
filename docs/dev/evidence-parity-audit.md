# Evidence Parity Audit

> **Classification:** Developer reference (derived from [diagnostics-evidence.md](../spec/diagnostics-evidence.md))
> **Audience:** Runtime developers, adapter authors, code reviewers.
> **Authority:** [diagnostics-evidence.md](../spec/diagnostics-evidence.md) is the normative specification. This document records adapter-level evidence parity findings, gaps, and a prioritized implementation list. If this document conflicts with the spec, the spec is correct.
> **Scope:** Operational runtime evidence produced by the four transport adapters (Matrix, Meshtastic, MeshCore, LXMF). Does not cover lifecycle, capability, SDK parity, or boundary documentation owned by other workers.
> **Branch:** `adapter-lifecycle-parity`
> **Baseline:** Post `adapter-sdk-parity` / after #99
> **Status:** Implementation audit synced with source/tests/docs

## 1. Summary

This audit compares how the four MEDRE adapters produce evidence through their `diagnostics()` methods and `health_check()` APIs. The [Diagnostics and Evidence Specification](../spec/diagnostics-evidence.md) §2 defines eight contractual common keys that SHALL appear in every adapter's `diagnostics()` output. This audit measures actual production against that contract, identifies gaps, inconsistencies, and misleading evidence, and ranks implementation opportunities by operational value.

**Key finding:** All four adapters now include both `health` and `mode` keys in
their `diagnostics()` output. Meshtastic, MeshCore, and LXMF all report `health`
via a cached `_last_health` value (set by `health_check()`), and `mode` via
their configured connection mode where applicable. Meshtastic and MeshCore
provide a full fallback `session` sub-dict when no session is active. LXMF's
`_session` is never set to `None` (created in `__init__`, retained through
`stop()`), so the session sub-dict is always present in practice. The
normalization layer (`diagnostic_contract.py`) resolves any remaining missing
keys to `None`, preserving JSON-safety. Remaining gaps: LXMF session fallback is
structurally absent (the `if self._session is not None` guard omits the key if
`_session` were ever `None`); queue evidence for Matrix and MeshCore; common-key
location normalization.

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

| Key                           | Expected type | Value source                                  | Actual type   | Status      | Notes                                                   |
| ----------------------------- | ------------- | --------------------------------------------- | ------------- | ----------- | ------------------------------------------------------- |
| `connected`                   | `bool`        | `diag.connected` (session dataclass)          | `bool`        | **present** | Top-level when session exists; `False` in fallback      |
| `health`                      | `str`         | `self._last_health` (cached health value)     | `str`         | **present** | Cached from `health_check()`; alias for spec common key |
| `mode`                        | `str`         | Hardcoded `"live"`                            | `str`         | **present** | Hardcoded in both session and fallback branches         |
| `reconnecting`                | `bool`        | `diag.reconnecting` (session dataclass)       | `bool`        | **present** | Sync recovery track                                     |
| `reconnect_attempts`          | `int`         | `diag.reconnect_attempts` (session dataclass) | `int`         | **present** |                                                         |
| `last_error`                  | `str or None` | `diag.last_sync_error`                        | `str or None` | **present** | Aliased as both `last_sync_error` and `last_error`      |
| `transient_delivery_failures` | `int`         | `self._transient_delivery_failures`           | `int`         | **present** | Adapter-level counter                                   |
| `permanent_delivery_failures` | `int`         | `self._permanent_delivery_failures`           | `int`         | **present** | Adapter-level counter                                   |

**Matrix assessment:** 8/8 keys fully conformant at top level (health included via cached `_last_health`; mode hardcoded `"live"`; `last_error` aliased from `last_sync_error`). Only `last_sync_error` is a Matrix-specific extension beyond the eight contractual keys.

#### Meshtastic

| Key                           | Expected type | Value source                               | Actual type   | Status               | Notes                                                                     |
| ----------------------------- | ------------- | ------------------------------------------ | ------------- | -------------------- | ------------------------------------------------------------------------- |
| `connected`                   | `bool`        | `session_diag.connected`                   | `bool`        | **present**          | In `session` sub-dict; fallback `False` when no session                   |
| `health`                      | `str`         | `self._last_health` (cached health value)  | `str or None` | **present**          | Cached from `health_check()`; cleared to `None` in `start()` and `stop()` |
| `mode`                        | `str`         | `self._config.connection_type`             | `str`         | **present**          | At adapter top level; e.g. `"fake"`, `"tcp"`, `"serial"`, `"ble"`         |
| `reconnecting`                | `bool`        | `session_diag.reconnecting`                | `bool`        | **present**          | In `session` sub-dict; fallback `False` when no session                   |
| `reconnect_attempts`          | `int`         | `session_diag.reconnect_attempts`          | `int`         | **present (nested)** | In `session` sub-dict                                                     |
| `last_error`                  | `str or None` | `session_diag.last_error`                  | `str or None` | **present (nested)** | In `session` sub-dict                                                     |
| `transient_delivery_failures` | `int`         | `session_diag.transient_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                                     |
| `permanent_delivery_failures` | `int`         | `session_diag.permanent_delivery_failures` | `int`         | **present (nested)** | In `session` sub-dict                                                     |

**Meshtastic assessment:** 8/8 common keys present. `health` and `mode` at adapter top level. Six remaining common keys in `session` sub-dict with full fallback when `self._session is None` (fallback includes `connected: False`, `reconnecting: False`, `reconnect_attempts: 0`, `last_error: None`, etc.). `_last_health` is cleared to `None` in both `start()` and `stop()`.

**Meshtastic as maturity reference:** Meshtastic has the richest queue evidence surface (14 queue-related keys: `queue_pending`, `queue_total_sent`, `queue_total_failed`, `queue_total_enqueued`, `queue_total_dequeued`, `queue_total_rejected`, `queue_total_requeued`, `queue_total_exhausted`, `queue_total_permanent_failed`, `queue_max_size`, `queue_send_max_attempts`, `queue_utilization_pct`, `queue_delay_between_messages`, `queue_last_send_time`). It also has the most complete classifier counter surface (12 counters) and startup backlog evidence.

#### MeshCore

| Key                           | Expected type | Value source                                            | Actual type   | Status      | Notes                                                                     |
| ----------------------------- | ------------- | ------------------------------------------------------- | ------------- | ----------- | ------------------------------------------------------------------------- |
| `connected`                   | `bool`        | `session.connected` (via `sanitize_diagnostic_mapping`) | `bool`        | **present** | In `session` sub-dict; fallback `False` when no session                   |
| `health`                      | `str`         | `self._last_health` (cached health value)               | `str or None` | **present** | Cached from `health_check()`; cleared to `None` in `start()` and `stop()` |
| `mode`                        | `str`         | `self._config.connection_type`                          | `str`         | **present** | At adapter top level AND in session sub-dict                              |
| `reconnecting`                | `bool`        | `session.reconnecting`                                  | `bool`        | **present** | In `session` sub-dict; fallback `False` when no session                   |
| `reconnect_attempts`          | `int`         | `session.reconnect_attempts`                            | `int`         | **present** | In `session` sub-dict; fallback `0` when no session                       |
| `last_error`                  | `str or None` | `session.last_error`                                    | `str or None` | **present** | In `session` sub-dict; fallback `None` when no session                    |
| `transient_delivery_failures` | `int`         | `session.transient_delivery_failures`                   | `int`         | **present** | In `session` sub-dict; fallback `0` when no session                       |
| `permanent_delivery_failures` | `int`         | `session.permanent_delivery_failures`                   | `int`         | **present** | In `session` sub-dict; fallback `0` when no session                       |

**MeshCore assessment:** 8/8 common keys present. `health` and `mode` at adapter top level. Six remaining common keys in `session` sub-dict with full fallback when `self._session is None`. `_last_health` is cleared to `None` in both `start()` and `stop()`. `diagnostics().health` may be `None` until `health_check()` is called again; this is intentional (no fresh health snapshot for the current lifecycle). `_inbound_dedup` is cleared in both `start()` (via `_reset_inbound_counters()`) and `stop()` (via `self._inbound_dedup.clear()`).

**`sdk_contact_timeout_count` (MeshCore transport-specific key):** Integer count
of contacts that have cached SDK `suggested_timeout` hints for DM retry delay
calculation. This is an aggregate-only diagnostic — it exposes the _count_ of
contacts in `_contact_retry_delays`, never the contact IDs (public key
prefixes), timeout values, or any identifying information. The underlying
`_contact_retry_delays` is `dict[str, float]` (keyed by contact ID, valued by
timeout in seconds), but `diagnostics()` returns only
`len(self._contact_retry_delays)`. This field is cleared on `stop()`,
failed-start cleanup (`_cleanup_failed_start()`), and at successful reconnect
boundaries. Operators can use this count to understand whether the SDK is
providing timeout hints and how many contacts are affected, without any
exposure of contact topology. This is a MeshCore-only transport-specific
diagnostic key, not a common key.

#### LXMF

| Key                           | Expected type | Value source                                | Actual type   | Status      | Notes                                                                      |
| ----------------------------- | ------------- | ------------------------------------------- | ------------- | ----------- | -------------------------------------------------------------------------- |
| `connected`                   | `bool`        | `self._session.connected`                   | `bool`        | **present** | In `session` sub-dict; `_session` never set to None, so key always present |
| `health`                      | `str`         | `self._last_health` (cached health value)   | `str or None` | **present** | Cached from `health_check()`; cleared to `None` in `start()` and `stop()`  |
| `mode`                        | `str`         | `self._config.connection_type`              | `str`         | **present** | At adapter top level AND in session sub-dict                               |
| `reconnecting`                | `bool`        | `self._session.reconnecting`                | `bool`        | **present** | In `session` sub-dict                                                      |
| `reconnect_attempts`          | `int`         | `self._session.reconnect_attempts`          | `int`         | **present** | In `session` sub-dict                                                      |
| `last_error`                  | `str or None` | `self._session.last_error`                  | `str or None` | **present** | In `session` sub-dict                                                      |
| `transient_delivery_failures` | `int`         | `self._session.transient_delivery_failures` | `int`         | **present** | In `session` sub-dict                                                      |
| `permanent_delivery_failures` | `int`         | `self._session.permanent_delivery_failures` | `int`         | **present** | In `session` sub-dict                                                      |

**LXMF assessment:** 8/8 common keys present. `health` and `mode` at adapter top level. Six remaining common keys in `session` sub-dict. `_session` is created in `__init__` and never set to `None`, so the session sub-dict is always present in practice. However, the code has a structural `if self._session is not None` guard that would omit the `session` key if `_session` were ever `None`; this is a defensive gap rather than an observed one. `_last_health` is cleared to `None` in both `start()` and `stop()`. `diagnostics().health` may be `None` until `health_check()` is called again; this is intentional (no fresh health snapshot for the current lifecycle). `_inbound_dedup` is cleared in `stop()` via `self._inbound_dedup.clear()`.

**LXMF spec/implementation note:** The spec (§3.4 LXMF note) states: "Session
diagnostics are exposed directly via the `LxmfSessionDiagnostics` frozen
dataclass. The LXMF adapter does not layer its own outer diagnostics dict on
top." The actual `LxmfAdapter.diagnostics()` implementation layers an outer dict
with `adapter_id`, `platform`, `started`, `mode`, and a `session` sub-dict. The
adapter now surfaces the spec-defined session fields (`last_message_time`,
`known_path_count`, `propagation_enabled`, `pending_delivery_count`) inside that
session sub-dict, preserving the current adapter-dict shape while closing the
missing-field gap.

## 5. Evidence Category Parity by Adapter

### 5.1 Diagnostics Evidence

| Aspect                          | Matrix             | Meshtastic                      | MeshCore                                    | LXMF                            |
| ------------------------------- | ------------------ | ------------------------------- | ------------------------------------------- | ------------------------------- |
| Common keys at top level        | 8 (all present)    | 2 (`health`, `mode`)            | 2 (`health`, `mode`)                        | 2 (`health`, `mode`)            |
| Common keys in session sub-dict | N/A (flat)         | 6 (all present)                 | 6 (all present)                             | 6 (all present)                 |
| Session-less fallback           | Full fallback dict | Full fallback dict              | Full fallback dict                          | Always present\*                |
| Transport-specific keys         | 21+                | 30+                             | 12+                                         | 3                               |
| Diagnostics shape               | Flat dict          | Adapter dict + session sub-dict | Adapter dict + session sub-dict (sanitized) | Adapter dict + session sub-dict |

\*LXMF `_session` is never set to `None` (created in `__init__`, retained through `stop()`), so the session sub-dict is always present in practice. The `if self._session is not None` guard is a structural defensive check, not an observed gap.

### 5.2 Queue Evidence

| Aspect       | Matrix | Meshtastic                                     | MeshCore | LXMF                               |
| ------------ | ------ | ---------------------------------------------- | -------- | ---------------------------------- |
| Queue depth  | None   | `queue_pending`                                | None     | `pending_delivery_count` (limited) |
| Send counts  | None   | `queue_total_sent`, `queue_total_failed`, etc. | None     | None                               |
| Queue health | None   | `queue` dict (utilization, timing)             | None     | None                               |
| Parity       | None   | **Reference implementation**                   | None     | Minimal                            |

**Gap:** MeshCore and Matrix produce zero queue evidence. Operators running
MeshCore or Matrix adapters have no visibility into outbound queue depth, send
throughput, or queue health. LXMF has minimal queue evidence through
`session.pending_delivery_count`.

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

| Aspect               | Matrix                 | Meshtastic                   | MeshCore    | LXMF       |
| -------------------- | ---------------------- | ---------------------------- | ----------- | ---------- |
| Classifier counters  | N/A (not packet-based) | 12 counters                  | 10 counters | N/A        |
| `inbound_published`  | Present                | Present                      | Present     | Present    |
| Suppression counters | 4 types                | 1 (startup backlog)          | None        | 3 counters |
| Startup backlog      | None                   | `startup_backlog_*` (3 keys) | None        | None       |

**Gap:** MeshCore has classifier counters but no suppression breakdown. LXMF now
exposes message-level ingress counters (`classifier_messages_seen`,
`classifier_messages_relayed`, `classifier_messages_ignored`,
`classifier_messages_ack_ignored`, `classifier_messages_non_text_ignored`,
`inbound_duplicates_suppressed`, `inbound_published`) rather than packet-class
counters.

### 5.7 Adapter Health Evidence

All adapters produce health through the same path:

1. `health_check()` returns `AdapterInfo` with a `health` string
2. `normalize_adapter_health()` projects to a normalized dict with `health`, `fake_or_live`, `capabilities`, `details`
3. Lifecycle state overrides apply (`INITIALIZING` → `starting`, `STOPPING` → `stopping`)
4. The six health vocabulary strings are enforced: `healthy`, `degraded`, `failed`, `unknown`, `starting`, `stopping`

**Assessment:** Health evidence is structurally uniform across adapters. All four adapters now include `health` in their `diagnostics()` output via cached `_last_health`. The `health_check()` API remains available for fresh health evaluation.

## 6. Identified Gaps, Inconsistencies, and Misleading Evidence

### 6.1 ~~`health` missing from diagnostics output~~ — RESOLVED

**Severity:** ~~High~~ Resolved
**Scope:** ~~Meshtastic, MeshCore, LXMF~~ All adapters
**Status:** Resolved. All four adapters now include `health` in their `diagnostics()` output. Meshtastic, MeshCore, and LXMF use a cached `_last_health` value set by `health_check()`, matching Matrix's pattern.

**Behavioral note:** All four adapters clear cached `_last_health` to `None` at start/stop lifecycle boundaries. `diagnostics().health` may be `None` until `health_check()` is called again. This is intentional: `None` means no fresh health snapshot has been taken for the current lifecycle.

### 6.2 ~~Session-less diagnostics fallback gap~~ — MOSTLY RESOLVED

**Severity:** ~~High~~ Resolved for Meshtastic and MeshCore; structural gap remains for LXMF
**Scope:** Meshtastic (resolved), MeshCore (resolved), LXMF (structural gap only)
**Status:** Meshtastic and MeshCore now provide full fallback `session` sub-dicts with safe defaults (`connected: False`, `reconnecting: False`, `reconnect_attempts: 0`, etc.) when `self._session is None`.

**LXMF structural note:** LXMF's `_session` is created in `__init__` and never set to `None`, so the session sub-dict is always present in practice. The `if self._session is not None` guard in `diagnostics()` would omit the `session` key if `_session` were ever `None`, but this path is never exercised. This is a defensive gap (no fallback dict) rather than an observed one.

### 6.3 ~~`mode` missing from real adapter diagnostics~~ — RESOLVED

**Severity:** ~~Medium~~ Resolved
**Scope:** ~~Meshtastic~~ All adapters
**Status:** Resolved. Meshtastic real adapter now includes `"mode": self._config.connection_type` at the adapter top level. All four adapters emit `mode` in `diagnostics()`.

### 6.4 ~~`last_error` naming inconsistency (Matrix)~~ RESOLVED

**Severity:** Low
**Scope:** Matrix only
**Status:** Resolved. Matrix adapter now emits both `last_sync_error` (Matrix-specific) and `last_error` (spec common key) as aliases of the same value. Cross-adapter tooling checking `last_error` now works correctly for Matrix.

### 6.5 Common-key nesting inconsistency

**Severity:** Low
**Scope:** All adapters
**Description:** Matrix exposes common keys at the adapter dict's top level. Meshtastic, MeshCore, and LXMF nest common keys in a `session` sub-dict. The spec (§2) allows both: "MAY appear directly or nested in a session sub-dict."

**Operator impact:** Tooling must handle both flat and nested layouts. The `normalize_diagnostics()` function handles this by extracting common keys from the flat dict only. Keys in the `session` sub-dict are treated as transport-specific, not common keys.

**Status:** Permitted by spec. The normalization layer handles it.

### 6.6 ~~LXMF spec/implementation missing fields~~ — RESOLVED

**Severity:** ~~Medium~~ Resolved
**Scope:** LXMF only
**Status:** Resolved. `LxmfAdapter.diagnostics()` now includes
`last_message_time`, `known_path_count`, `propagation_enabled`,
and `pending_delivery_count` in the `session` sub-dict.

**Shape note:** The adapter still returns an outer diagnostics dict with a
nested `session` sub-dict. This differs from the spec note that describes direct
dataclass exposure, but the missing operational fields are now visible.

### 6.7 Misleading evidence: `connected: true` does not guarantee next-operation success

**Severity:** Informational
**Scope:** All adapters
**Description:** This is an observational caveat, not a bug. The spec (§12) explicitly states that `connected: true` is a point-in-time snapshot. All diagnostics are observational and do not guarantee future operation success. This is correctly documented.

## 7. Prioritized Implementation List

Ranked by operational value — the value each fix provides to operators diagnosing issues in production or pre-production scenarios. Meshtastic is used as the maturity reference where noted.

### Priority 1 (P0): ~~No-session fallback completeness~~ — RESOLVED

**Adapters:** ~~Meshtastic, MeshCore, LXMF~~ Meshtastic and MeshCore resolved. LXMF structural gap (never exercised).
**Status:** Resolved. Meshtastic and MeshCore now provide full fallback dicts when `self._session is None`. LXMF's `_session` is never `None` in practice.

### Priority 2 (P0): ~~`health` key in diagnostics output~~ — RESOLVED

**Adapters:** ~~Meshtastic, MeshCore, LXMF~~ All adapters resolved.
**Status:** Resolved. All four adapters now include `health` via cached `_last_health` in `diagnostics()`. All four clear `_last_health` to `None` in both `start()` and `stop()`; `diagnostics().health` may be `None` until `health_check()` is called again.

### Priority 3 (P1): ~~`mode` key in real adapter diagnostics~~ — RESOLVED

**Adapters:** ~~Meshtastic~~ All adapters resolved.
**Status:** Resolved. All four adapters now include `mode` in `diagnostics()`.

### Priority 4 (P1): ~~`last_error` normalization for Matrix~~ RESOLVED

**Adapters:** Matrix
**Status:** Resolved. Matrix adapter now emits both `last_sync_error` (Matrix-specific) and `last_error` (spec common key) as aliases of the same value. Cross-adapter tooling checking `last_error` now works correctly for Matrix.

### Priority 5 (P2): ~~LXMF adapter diagnostics completeness~~ — RESOLVED

**Adapters:** LXMF
**Status:** Resolved. `LxmfAdapter.diagnostics()` surfaces
`last_message_time`, `known_path_count`, `propagation_enabled`,
and `pending_delivery_count` from `LxmfSessionDiagnostics` in the
adapter's `session` sub-dict.
**Tests:** `tests/test_lxmf_diagnostics_parity.py::test_lxmf_adapter_exposes_session_diagnostics_fields`.

### Priority 6 (P2): Queue evidence for MeshCore and Matrix

**Adapters:** MeshCore, Matrix
**Fix:** Add basic queue evidence (`queue_pending`, `queue_total_sent`, `queue_total_failed`) to adapter diagnostics, using Meshtastic as the reference pattern.
**Operational value:** Medium. Meshtastic's queue evidence is the most operationally valuable evidence surface for understanding outbound delivery pressure. MeshCore and Matrix have no queue visibility. Operators running these adapters cannot diagnose send backpressure or queue stalls.
**Constraint:** Matrix uses nio's sync loop rather than a discrete queue. Queue evidence for Matrix would need to track in-flight delivery promises differently than Meshtastic's queue system. MeshCore may not have a discrete queue either.
**Estimated effort:** Medium. Requires defining what "queue" means for each transport.

### Priority 7 (P2): ~~Ingress evidence parity for LXMF~~ — RESOLVED

**Adapters:** LXMF
**Status:** Resolved. LXMF exposes message-level ingress counters for
seen, relayed, ignored, ACK ignored, non-text ignored, duplicate
suppression, and published messages.
**Tests:** `tests/test_lxmf_diagnostics_parity.py::test_lxmf_adapter_ingress_counters_are_auditable`
and
`tests/test_lxmf_diagnostics_parity.py::test_lxmf_ingress_counters_reset_on_start`.

### Priority 8 (P3): Common-key location normalization

**Adapters:** All four
**Fix:** Either hoist session sub-dict common keys to the adapter's top level, or document that the normalization layer handles extraction and operators should use `normalize_diagnostics()` consistently.
**Operational value:** Low. The normalization layer already handles this. Direct consumers of adapter `diagnostics()` see inconsistency, but the `RuntimeSnapshot` and evidence bundle normalize the view.
**Estimated effort:** Low to medium, depending on approach.

### Priority 9 (P3): ~~`_last_health` clearing consistency~~ — RESOLVED

**Adapters:** ~~MeshCore, LXMF~~ All adapters
**Status:** Resolved. All four adapters now clear `_last_health` to `None` in both `start()` and `stop()`. `diagnostics().health` may be `None` until `health_check()` is called again; this is intentional.

## 8. Constraints Observed

This audit observes the following constraints, which any implementation work must respect:

1. **Adapters report facts only.** No adapter inventories success, infers state, or performs health polling through diagnostics.
2. **Runtime evidence must match actual guarantees.** The observational caveat (spec §12) applies: diagnostics are point-in-time snapshots, not authoritative state.
3. **Capability/evidence must not overclaim behavior.** Evidence does not prove reliability, ordering, latency bounds, or production readiness.
4. **No schema churn.** This audit recommends filling gaps, not changing existing key shapes or types.
5. **No compatibility layers.** Implementation fixes should be direct, not shimmed.
6. **No new evidence schemas.** The eight contractual keys are stable. New keys go into transport-specific sections.

## 9. Sources Examined

| Source                                              | Purpose                                                                                   |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `docs/spec/diagnostics-evidence.md`                 | Normative contract for 8 common keys, evidence classification, observational caveat       |
| `src/medre/core/evidence/adapter_status.py`         | `AdapterStatusEvidence` dataclass, operator status derivation                             |
| `src/medre/core/supervision/diagnostic_contract.py` | `normalize_diagnostics()`, `COMMON_DIAGNOSTIC_KEYS`, sanitization                         |
| `docs/dev/runtime-evidence-completeness-audit.md`   | Runtime evidence surface inventory, event taxonomy                                        |
| `docs/dev/testing.md`                               | Testing rules, file size limits, evidence honesty                                         |
| `tests/test_adapter_health.py`                      | Health normalization tests, vocabulary coverage                                           |
| `tests/test_adapter_status_evidence.py`             | Operator status derivation, lifecycle state mapping                                       |
| `src/medre/adapters/matrix/adapter.py`              | `MatrixAdapter.diagnostics()` method                                                      |
| `src/medre/adapters/matrix/session.py`              | `MatrixSessionDiagnostics` dataclass, `MatrixSession.diagnostics()` method                |
| `src/medre/adapters/meshtastic/adapter.py`          | `MeshtasticAdapter.diagnostics()` method                                                  |
| `src/medre/adapters/meshtastic/session.py`          | `MeshtasticSessionDiagnostics` dataclass, `MeshtasticSession.diagnostics()` method        |
| `src/medre/adapters/meshcore/adapter.py`            | `MeshCoreAdapter.diagnostics()` method                                                    |
| `src/medre/adapters/meshcore/session.py`            | `_SessionDiagnostics` dataclass, `MeshCoreSession.diagnostics()` method                   |
| `src/medre/adapters/lxmf/adapter.py`                | `LxmfAdapter.diagnostics()` method                                                        |
| `src/medre/adapters/lxmf/session.py`                | `LxmfSessionDiagnostics` / `_SessionDiagnostics` dataclasses, `LxmfSession.diagnostics()` |

## 10. Validation Surfaces

The following test areas guard the evidence contracts audited here:

- `test_adapter_health.py`: Health vocabulary coverage, normalization structure, lifecycle state override, fake/live detection
- `test_adapter_status_evidence.py`: Operator status derivation, input tolerance, serialization, lifecycle state mapping
- `test_runtime_snapshot.py`: Snapshot construction, adapter ordering, JSON-safety, key sorting
- `test_evidence_bundle.py`: Bundle shape, section statuses, hoisted fields, schema version
- `test_diagnostic_contract.py`: Common key extraction, sanitization, missing key fallback

Any implementation work stemming from this audit should extend these test surfaces rather than creating new ones, respecting the file size limits in testing.md.
