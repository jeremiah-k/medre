# Transport Validation Matrix

> Last updated: 2026-05-15
> Scope: Single authoritative source of truth for transport validation evidence
> Status: Pre-beta. Evidence claims are honest per-transport.

This document is the authoritative record of what validation evidence exists for
each MEDRE transport adapter, at each tier of fidelity. It consolidates and
supersedes the transport evidence tables previously in
`docs/architecture/adapter-ingress-audit.md` and
`docs/runbooks/fake-bridge-evidence-criteria.md`.

For operator guidance on running validation tests, see
[docs/runbooks/bridge-operation.md](../runbooks/bridge-operation.md) section 8.


## Legend

| Tier | Definition | What passing proves | What passing does NOT prove |
|------|-----------|---------------------|---------------------------|
| **Fake adapter bridge** | `simulate_inbound` through real codec and pipeline to fake outbound adapter | Pipeline routing, rendering, receipts, accounting, loop prevention all work end-to-end | Nothing about real SDK behavior, network I/O, or hardware |
| **Wrapper callback bridge** | Real adapter callback (`_on_packet` / `_on_room_message`) invoked directly with simulated data, through codec and pipeline to fake outbound | Adapter callback path, codec decode, pipeline routing, accounting work with real adapter code | Nothing about real SDK subscription chains, pubsub delivery, or network I/O |
| **Docker SDK lifecycle** | Real SDK library connecting to a containerized service (Synapse, meshtasticd). Start, stop, health, diagnostics. | Dependency resolution, config loading, adapter lifecycle, real SDK init/connect/stop work against a real service process | Nothing about inbound event delivery through SDK callbacks (unless separately proven). Container runs on localhost, not a real network. |
| **Docker inbound** | Real SDK receives inbound events from the containerized service through its normal subscription mechanism | Full SDK callback chain fires: service publishes event, SDK subscription delivers it, adapter callback processes it, pipeline routes it | Container runs on localhost. Not real network/hardware. |
| **Live network/radio** | Real SDK connecting to a real endpoint (homeserver account, LoRa radio hardware, Reticulum network) | Actual connectivity, protocol compliance, real-world message delivery | Does not prove sustained reliability, throughput, or reconnect resilience. Evidence is smoke-test level. |


## Evidence Matrix

| Evidence tier | Matrix | Meshtastic | MeshCore | LXMF |
|---|:---:|:---:|:---:|:---:|
| Fake adapter bridge | ✅ proven | ✅ proven | ✅ proven | ✅ proven |
| Wrapper callback bridge | ✅ proven | ✅ proven | ✅ proven | ✅ proven |
| Docker SDK lifecycle | ✅ proven | ✅ proven (outbound only) | ❌ not set up | ❌ not set up |
| Docker inbound | ✅ proven (sync_loop) | ❓ unconfirmed | ❌ not set up | ❌ not set up |
| Live network/radio | ✅ proven (Synapse) | ❌ not claimed | ❌ not claimed | ❌ not claimed |


## Per-Adapter Detail

### Matrix

| Tier | Status | Evidence | Test files | Notes |
|------|--------|----------|-----------|-------|
| Fake adapter bridge | ✅ proven | Full pipeline: simulate_inbound → codec → routing → fake outbound delivery | `tests/test_matrix_fake_bridge.py`, `tests/test_fake_bridge_smoke.py` | |
| Wrapper callback bridge | ✅ proven | `_on_room_message` invoked directly → codec → pipeline → fake outbound | `tests/test_matrix_wrapper_ingress.py` | |
| Docker SDK lifecycle | ✅ proven | Real nio SDK connects to Docker Synapse. Start, health, deliver, stop. | `tests/integration/test_synapse_connectivity.py` | Synapse runs on localhost |
| Docker inbound | ✅ proven (sync_loop) | Real nio `sync_forever` delivers inbound event through `_on_room_message` callback. Pipeline routes to fake outbound. Receipts persisted with genuine Synapse `event_id`. | `tests/integration/test_synapse_bridge_smoke.py`, `tests/integration/test_synapse_run_session.py` | Bridge smoke tracks `ingress_path`: `"sync_loop"` (proven) vs `"direct_on_room_message_fallback"` (weaker). Run-session test exercises full runtime lifecycle. |
| Live network/radio | ✅ proven (Synapse) | Real Matrix account sends message to real homeserver. | `tests/test_matrix_live.py` (requires `MATRIX_*` env vars, gated by `@require_live`) | Smoke test only. Not sustained or reliability testing. |

### Meshtastic

| Tier | Status | Evidence | Test files | Notes |
|------|--------|----------|-----------|-------|
| Fake adapter bridge | ✅ proven | Full pipeline: simulate_inbound → codec → routing → fake outbound delivery | `tests/test_meshtastic_fake_bridge.py`, `tests/test_fake_bridge_smoke.py` | |
| Wrapper callback bridge | ✅ proven | `_on_packet` invoked directly → classify → codec.decode → publish_inbound → pipeline → fake outbound | `tests/test_meshtastic_wrapper_ingress.py` | |
| Docker SDK lifecycle | ✅ proven (outbound only) | Real `mtjk` SDK creates `TCPInterface` to containerized meshtasticd. Adapter subscribes to `meshtastic.receive` pubsub, sends via real `sendText`, reports healthy, stops cleanly. Returns real packet ID. | `tests/integration/test_meshtasticd_connectivity.py`, `tests/integration/test_meshtasticd_sdk_bridge.py` | meshtasticd runs with `-s` (simulation mode). Not real LoRa. |
| Docker inbound | ❓ unconfirmed | Would require: second client sends → meshtasticd relays → pubsub fires → `_on_receive` → `_on_packet` → codec → `publish_inbound`. | `tests/integration/test_meshtasticd_sdk_bridge.py` (`test_two_client_real_packet_injection`, xfail) | meshtasticd simulation mode may not relay packets between TCP clients. Test is `xfail(strict=False)` — bonus evidence when it passes, but does not reliably pass. Inbound in Docker tests uses `simulate_inbound()`, not real pubsub delivery. |
| Live network/radio | ❌ not claimed | No live hardware smoke test recorded. | None | |

Known gap: meshtasticd two-client relay unconfirmed. The `test_simulate_inbound_bridge_to_fake_outbound` test proves the codec/pipeline/accounting path works while a real meshtasticd session is active, but inbound packets are injected via `simulate_inbound()`, not received through the `meshtastic.receive` pubsub callback.

### MeshCore

| Tier | Status | Evidence | Test files | Notes |
|------|--------|----------|-----------|-------|
| Fake adapter bridge | ✅ proven | Full pipeline: simulate_inbound → codec → routing → fake outbound delivery | `tests/test_fake_bridge_smoke.py` | |
| Wrapper callback bridge | ✅ proven | `_on_message` invoked directly → classify → codec.decode → publish_inbound → pipeline → fake outbound | `tests/test_meshcore_wrapper_ingress.py` | |
| Docker SDK lifecycle | ❌ not set up | No containerized MeshCore node exists. | None | No Docker setup for MeshCore. |
| Docker inbound | ❌ not set up | No Docker setup. | None | |
| Live network/radio | ❌ not claimed | No live hardware smoke test recorded. | None | |

Known gap: No Docker SDK-boundary or live validation. Unit-tested only.

### LXMF

| Tier | Status | Evidence | Test files | Notes |
|------|--------|----------|-----------|-------|
| Fake adapter bridge | ✅ proven | Full pipeline: simulate_inbound → codec → routing → fake outbound delivery | `tests/test_fake_bridge_smoke.py` | |
| Wrapper callback bridge | ✅ proven | `_on_packet` invoked directly → classify → codec.decode → publish_inbound → pipeline → fake outbound | `tests/test_lxmf_wrapper_ingress.py` | |
| Docker SDK lifecycle | ❌ not set up | No containerized Reticulum/LXMF router exists. | None | No Docker setup for LXMF. |
| Docker inbound | ❌ not set up | No Docker setup. | None | |
| Live network/radio | ❌ not claimed | No live network smoke test recorded. | None | |

Known gap: No Docker SDK-boundary or live validation. Unit-tested only.


## Cross-Reference: Test File Index

### Unit tests (no Docker, no hardware)

| File | Adapters covered | What it tests |
|------|-----------------|---------------|
| `tests/test_fake_bridge_smoke.py` | All four | Fake adapter bridge: simulate_inbound → pipeline → fake outbound |
| `tests/test_matrix_fake_bridge.py` | Matrix | Matrix-specific fake bridge scenarios |
| `tests/test_meshtastic_fake_bridge.py` | Meshtastic | Meshtastic-specific fake bridge scenarios |
| `tests/test_matrix_wrapper_ingress.py` | Matrix | Wrapper callback: `_on_room_message` → pipeline → fake outbound |
| `tests/test_meshtastic_wrapper_ingress.py` | Meshtastic | Wrapper callback: `_on_packet` → pipeline → fake outbound |
| `tests/test_meshcore_wrapper_ingress.py` | MeshCore | Wrapper callback: `_on_message` → pipeline → fake outbound |
| `tests/test_lxmf_wrapper_ingress.py` | LXMF | Wrapper callback: `_on_packet` → pipeline → fake outbound |
| `tests/test_wrapper_multi_callback.py` | All four | Multiple callbacks across adapters in one pipeline |

### Docker integration tests (require Docker)

| File | Adapters covered | What it tests |
|------|-----------------|---------------|
| `tests/integration/test_synapse_connectivity.py` | Matrix | Docker SDK lifecycle: connect, health, deliver, stop |
| `tests/integration/test_synapse_bridge_smoke.py` | Matrix | Docker inbound: sync_loop delivers event through pipeline with real Synapse event_ids |
| `tests/integration/test_synapse_run_session.py` | Matrix | Full runtime lifecycle against Docker Synapse |
| `tests/integration/test_meshtasticd_connectivity.py` | Meshtastic | Docker SDK lifecycle: TCPInterface to containerized meshtasticd |
| `tests/integration/test_meshtasticd_sdk_bridge.py` | Meshtastic | Docker outbound + lifecycle + simulate_inbound while meshtasticd active; two-client relay xfail |

### Live tests (require real credentials/hardware)

| File | Adapters covered | What it tests |
|------|-----------------|---------------|
| `tests/test_matrix_live.py` | Matrix | Live Matrix smoke against real homeserver (gated by `@require_live`, requires `MATRIX_*` env vars) |


## Summary of Known Gaps

| Gap | Affected adapter(s) | Impact |
|-----|---------------------|--------|
| meshtasticd two-client relay | Meshtastic | Inbound delivery through real pubsub callback unconfirmed. Docker tests inject via `simulate_inbound`. |
| No Docker setup | MeshCore, LXMF | No evidence that real SDK connects to any service. Adapter code validated only through unit tests and wrapper callbacks. |
| No live radio/network | Meshtastic, MeshCore, LXMF | No evidence that adapters work with real hardware or live networks. May have fundamental issues. |
| No live cross-transport bridge | All | No test routes events between two real adapters. Bridge smoke routes real Matrix to fake outbound. |
| No third-party Matrix inbound | Matrix | Bridge smoke uses HTTP API sender, not a second Matrix client. Inbound from a different user is unconfirmed. |


## Relationship to Other Documents

- **`docs/architecture/adapter-ingress-audit.md`** — detailed audit of inbound callback paths, self-message filtering, duplicate handling, and per-adapter code paths. This matrix summarizes the validation evidence; the audit describes the code architecture.
- **`docs/runbooks/fake-bridge-evidence-criteria.md`** — provenance tier definitions and assertion criteria for fake bridge and Docker SDK-boundary levels. Simpler provenance table with a cross-reference to this matrix.
- **`docs/runbooks/bridge-operation.md`** — operational runbook including per-transport delivery semantics and section 8 validation guidance.
- **`docs/contracts/37-transport-maturity-classification.md`** — per-transport maturity tier and evidence requirements.
