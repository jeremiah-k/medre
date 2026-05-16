# Alpha Readiness Gap Audit

> Document version: 1
> Last updated: 2026-05-15
> Status: Honest assessment. No overclaims.

This document records what is NOT ready, what is partially ready, and what
remains unproven as of the alpha milestone. It is organized by layer so that
any reviewer can see at a glance where confidence ends and where assumptions
begin.

---

## Transport

### Matrix

| Gap | Status | Evidence |
|-----|--------|----------|
| Docker sync-loop strictness | xfail guard exists | `test_synapse_bridge_smoke.py` marks xfail if fallback sync strategy used instead of strict sync_loop. Proves the test exists and tracks progress toward the goal. |
| Live Synapse smoke | Proven (smoke only) | Live test sends one message to a real Synapse instance and verifies it arrives. No sustained throughput, no reconnect resilience. |
| Third-party inbound | Unconfirmed | No live test has verified inbound message reception from a second Matrix account. |
| Encrypted rooms | Unit-tested only | E2EE path tested with fake adapters. No live encrypted-room smoke test recorded. |

### Meshtastic

| Gap | Status | Evidence |
|-----|--------|----------|
| Docker outbound | Proven | `test_meshtasticd_sdk_bridge.py` exercises outbound delivery against containerized `meshtasticd`. |
| Inbound from 2nd client | Unconfirmed (xfail) | No live test exercises inbound message reception from a second Meshtastic client. |
| Live radio | None | No test against real LoRa hardware through medre. |

### MeshCore

| Gap | Status | Evidence |
|-----|--------|----------|
| Wrapper callback | Proven (unit) | Unit tests verify the MeshCore callback wrapper invokes correctly. |
| Docker setup | None | No Docker-based MeshCore integration test exists. |
| Live radio | None | No test against real MeshCore hardware through medre. |

### LXMF

| Gap | Status | Evidence |
|-----|--------|----------|
| Wrapper callback | Proven (unit) | Unit tests verify the LXMF callback wrapper invokes correctly. |
| Reticulum setup | None | No Docker-based or local Reticulum integration test exists. |
| Live radio | None | No test against real Reticulum/LXMF hardware through medre. |

---

## Runtime

### Retry

| Gap | Status | Detail |
|-----|--------|--------|
| RetryWorker | Exists, opt-in only | Disabled by default. Activates only when a `RetryPolicy` is configured on the route or delivery plan. |
| Scope | adapter_transient only | RetryWorker handles `ADAPTER_TRANSIENT` failures only. No retry for permanent failures, renderer failures, or planner failures. |
| Active adapter restart | Not implemented | If an adapter crashes mid-delivery, the runtime does not restart it. Operator must restart the process. |
| Final delivery ACK | Not implemented | RetryWorker re-attempts delivery but does not confirm the remote side received the message. |

### Replay

| Gap | Status | Detail |
|-----|--------|--------|
| Trigger mechanism | Manual operator action only | Replay is initiated by the operator via `medre replay` CLI command. No automatic replay trigger exists. |
| Duplicate risk | Present | BEST_EFFORT replay may produce duplicate sends. No storage-level deduplication. |
| Dedupe | Not implemented | The `replay_run_id` and `source` columns support traceability (post-incident investigation) but do not prevent or detect duplicate sends at delivery time. |
| Progress tracking | None | No resume-from-last-position capability. A failed replay run must be re-executed from the start. |

### Accounting

| Gap | Status | Detail |
|-----|--------|--------|
| Scope | Process-local only | Counters and gauges live in memory within the runtime process. |
| Persistence | None | Accounting state resets on process restart. No snapshot-to-disk, no reload. |
| Export | None | No Prometheus, statsd, or OTLP export. Only accessible via `medre diagnostics` while the process is running. |

### Storage

| Gap | Status | Detail |
|-----|--------|--------|
| Engine | SQLite only | No PostgreSQL, no network database. SQLite file on local disk. |
| Schema version | v1 (pre-release) | `CURRENT_SCHEMA_VERSION = 1`. No migration pipeline exists. The `_MigrationRegistry` is registry-only (no automatic migration). |
| DB recreation policy | TBD before release | No documented policy on whether schema changes require DB recreation vs migration. This decision must be made before any stable release. |
| Compaction | None | No WAL checkpoint, VACUUM, or retention policy for old receipt data. |

---

## Tests

### Legacy test files (pre-architecture, high line count)

| File | Lines | Test functions | Note |
|------|-------|---------------|------|
| `test_cli.py` | 2172 | 136 | CLI command integration tests |
| `test_matrix_session.py` | 2241 | 107 | Matrix session lifecycle tests |
| `test_storage.py` | 2253 | 84 | Storage contract and query tests |
| `test_canonical_events.py` | 1992 | 154 | Canonical event construction and round-trip |
| `test_replay_routing.py` | 1584 | 53 | Replay routing and planning tests |
| `test_meshtastic_fake_bridge.py` | 1540 | 21 | Meshtastic fake bridge pipeline |
| `test_fake_runtime_smoke.py` | 1506 | 47 | Fake adapter runtime smoke tests |
| **Total** | **13288** | **502** | |

These files predate the current architecture and contain a mix of unit and
integration-level tests. They are not broken, but they are large, monolithic,
and make refactoring harder. They do not need to be rewritten before alpha, but
their existence is a maintenance risk.

### Test infrastructure gaps

| Gap | Status | Detail |
|-----|--------|--------|
| Docker tests | Opt-in only | Tagged `@pytest.mark.docker`. Not run in default `pytest` invocation. Require local Docker. |
| Live tests | Manual only | Tagged `@pytest.mark.live`. Require environment variables for credentials and endpoints. Not automated. |
| Long-run soak | None | The `test_soak_harness.py` exercises stability patterns in seconds using fake adapters. No automated multi-hour soak with real transports exists. |
| Coverage enforcement | None | No minimum coverage threshold in CI. Coverage reports exist but are informational. |

---

## Operator

| Gap | Status | Detail |
|-----|--------|--------|
| First-run config | Exists, manual edit required | `medre config sample` generates a TOML file. Operator must edit it to declare adapters and routes. No interactive config wizard. |
| Env var documentation | Exists | Documented in config file headers and `docs/runbooks/configuration.md`. |
| Automated operator workflow | None outside test suite | No guided setup, no health dashboard, no operational playbook beyond this runbook. The test suite (`medre smoke`, `medre evidence`) is the closest thing to an operator verification workflow. |
| Error messages | Present but rough | Error messages exist but are not always actionable. Config validation errors point to the right field but may not explain the fix. |
| Log format | Structured but basic | Logs use Python logging with structured fields. No JSON log format option, no log level filtering by subsystem. |

---

## Summary

What is proven at alpha:

- Pipeline routing, storage, trace, and evidence collection work end-to-end with
  fake adapters (3,200+ tests passing).
- Matrix outbound delivery works against a real Synapse instance (smoke only).
- Meshtastic outbound delivery works against a containerized `meshtasticd`.
- RetryWorker exists for opt-in transient-failure retry (unit-tested, not live-tested).
- Replay engine supports five modes with deterministic behavior (unit-tested).

What is NOT proven at alpha:

- No transport is proven under sustained load.
- No transport has proven reconnect resilience.
- Two transports (MeshCore, LXMF) have zero live evidence.
- No adapter restarts on crash.
- No replay deduplication. Native-ref dedup (Stage 1.5) prevents echo loops at the pipeline level, but BEST_EFFORT replay produces duplicate sends and no storage-level dedup exists.
- No accounting survives a process restart.
- No automated operational workflow exists.
