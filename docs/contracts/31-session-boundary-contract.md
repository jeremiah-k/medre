# Session Boundary Contract

> Contract version: 1
> Last updated: 2026-05-09
> Track: 9 (Transport Capability Contracts)
> Supersedes: Nothing. Formalizes findings from contracts 27, 28.
> Status: Contract. Documents session ownership boundaries for beta.

This document defines the ownership boundaries of each transport session: what responsibilities sessions own, what they must not own, size and complexity risks, split candidates for future extraction, and explicit future extraction boundaries. It is a boundary contract, not a refactor plan. No broad refactor is proposed now.

This is a contract document. No session restructuring, adapter abstraction, or runtime redesign is proposed.


## 1. Scope

- What each session owns and is responsible for.
- What each session must not own.
- Size and complexity risks per session.
- Split candidates (if any) for future extraction.
- Future extraction boundaries (documented, not implemented).
- Per-session status assessment.

## 2. Non-goals

- Refactoring sessions now.
- Normalizing session shapes across transports.
- Extracting shared session abstractions or base classes.
- Building runtime-level session orchestration.


## 3. Responsibilities Sessions Own

Each session owns its transport SDK lifecycle end to end. The adapter delegates all SDK interaction to the session. The adapter owns semantic conversion (codec, routing, event publishing); the session owns raw transport management.

### 3.1 Common Owned Responsibilities

All four sessions own these responsibilities:

| Responsibility | Description |
|---------------|-------------|
| SDK client lifecycle | Construction, initialization, teardown of the SDK client object |
| Connection management | Establishing and maintaining the transport connection |
| Callback registration | Registering transport-level callbacks/subscriptions internally |
| Inbound forwarding | Forwarding received messages to the adapter-provided `message_callback` |
| Bounded reconnect | Exponential backoff reconnect with max 10 attempts |
| Outbound send | Sending messages through the transport SDK |
| Send retry | Bounded retry (up to 3 attempts) for transient send failures |
| Diagnostics | Providing a read-only snapshot of session operational state |
| Graceful teardown | Clean shutdown of SDK client, cancellation of background tasks |

### 3.2 Per-Session Additional Owned Responsibilities

| Session | Additional Owned Responsibilities |
|---------|----------------------------------|
| MatrixSession | nio `AsyncClient` lifecycle, login restoration, sync loop management, E2EE crypto lifecycle, room encryption state tracking, crypto store continuity |
| MeshtasticSession | Interface type dispatch (TCP/serial/BLE), pubsub callback wiring |
| MeshCoreSession | Connection type dispatch (TCP/serial/BLE), event subscription management |
| LxmfSession | Reticulum lifecycle, identity loading, LXMRouter initialization, delivery state model (8 states), outbound delivery map, state update callbacks, delivery state progression tracking |


## 4. Responsibilities Sessions Must Not Own

These boundaries are contractual. Sessions must not absorb these responsibilities, now or in future extraction.

| Responsibility | Owner | Rationale |
|---------------|-------|-----------|
| Canonical event construction | Codec | Sessions receive and forward raw/normalized dicts. They never construct `CanonicalEvent`. |
| Routing decisions | Routing layer | Sessions never decide where messages go. |
| Delivery receipt recording | Pipeline | Sessions never record receipts or interact with storage. |
| Bridge policy evaluation | Bridge policy layer (not yet built) | Sessions are transport-tunnel endpoints, not routing decision makers. |
| Health polling or circuit breaking | Runtime health layer | Sessions report state; they do not implement health polling loops. |
| Rate limiting or backpressure | Pipeline/runtime | Sessions send when asked. They do not throttle or reject based on load. |
| Plugin hook dispatch | Plugin system (if built) | Sessions are not extension points. |
| Secret storage or rotation | Config/external | Sessions receive config; they do not manage secret lifecycles. |
| Cross-transport coordination | Runtime (future) | Sessions are isolated per transport. No cross-session coordination. |


## 5. Size and Complexity Risks

### 5.1 Current Sizes

| Session | LOC | Complexity Assessment |
|---------|-----|----------------------|
| MatrixSession | 682 | Moderate. E2EE lifecycle adds complexity but is well-contained. |
| MeshtasticSession | 608 | Low-moderate. Straightforward connection dispatch and send. |
| MeshCoreSession | 654 | Low-moderate. Simplest conceptual model, similar to Meshtastic. |
| LxmfSession | 1260 | High. Roughly 2x the size of the other three. |

### 5.2 Risk Assessment

**MatrixSession (682 LOC):** Acceptable size. The E2EE lifecycle (crypto store loading, encryption mode management, undecryptable event counting) is contained within the session. No immediate risk. The 60s backoff cap diverges from the 30s cap on the other three sessions; this is a minor inconsistency, not a risk.

**MeshtasticSession (608 LOC):** Acceptable size. The outbound send with retry is straightforward. The only complexity is the interface type dispatch for TCP/serial/BLE connections.

**MeshCoreSession (654 LOC):** Acceptable size. Very similar shape to MeshtasticSession. Connection type dispatch and event subscription are simple.

**LxmfSession (1260 LOC):** This is the primary size/complexity risk. The honest delivery state model (8 states, individual outbound delivery tracking, state change callbacks) accounts for roughly half the session's complexity. This is an inherent property of LXMF's async store-and-forward architecture, not a design flaw. However, the size makes the session harder to reason about and test in isolation.


## 6. Split Candidates

These are documented for future consideration, not for immediate action.

### 6.1 LxmfSession: Delivery State Tracker Extraction

The most viable extraction candidate. The delivery state tracking logic (outbound delivery map, state transition callbacks, state progression recording) could be extracted into a standalone `_DeliveryTracker` class.

**Current location:** Inline within `LxmfSession`, mixed with connection lifecycle code.

**Extraction boundary:** The tracker would accept state update callbacks and manage the outbound delivery map. The session would delegate state tracking to the tracker instance. The session would still own the LXMRouter lifecycle and send/receive paths.

**Estimated LOC reduction:** ~200-300 LOC from the session, into a self-contained class.

**Risk:** Low. The delivery tracking logic has clear inputs (SDK state update callbacks) and outputs (diagnostics counters, state queries). The extraction boundary is well-defined.

**Priority:** Not blocking. This is a future cleanup, not a beta blocker.

### 6.2 MatrixSession: E2EE Lifecycle Extraction

The E2EE management code (crypto store loading, encryption mode detection, undecryptable event counting, room encryption state tracking) could be extracted into an `_E2EEManager` class.

**Current location:** Inline within `MatrixSession`, intermixed with sync loop management.

**Extraction boundary:** The manager would own crypto store path validation, device ID configuration, encryption mode resolution, and room encryption state tracking. The session would delegate E2EE concerns to the manager.

**Estimated LOC reduction:** ~100-150 LOC from the session.

**Risk:** Moderate. E2EE state is entangled with the sync loop (encrypted rooms are discovered during sync). The extraction boundary is less clean than the LXMF delivery tracker.

**Priority:** Low. Only worth doing if E2EE complexity grows significantly.

### 6.3 MeshtasticSession: No Viable Split

At 608 LOC with straightforward logic, there is no viable extraction candidate. The session is a single cohesive unit.

### 6.4 MeshCoreSession: No Viable Split

Same assessment as MeshtasticSession. The session is cohesive and small enough.


## 7. Future Extraction Boundaries

These boundaries are documented so that future work does not accidentally violate session encapsulation.

### 7.1 Session-to-Adapter Boundary

The adapter provides a `message_callback` to the session constructor. The session calls this callback with normalized plain dicts. The session never receives or returns `CanonicalEvent` instances. This boundary is clean and must remain so.

### 7.2 Session-to-SDK Boundary

The session is the sole owner of the SDK client object. No other module in the adapter package imports or touches the SDK directly. This boundary is enforced by convention and must remain so.

### 7.3 Session-to-Diagnostics Boundary

Sessions expose `diagnostics()` returning either a frozen dataclass or a plain dict copy. The diagnostics are read-only snapshots. No consumer should attempt to modify session state through diagnostics. The frozen dataclass pattern (Matrix, Meshtastic, LXMF) enforces this at the type level. MeshCore's plain dict copy is mutable but the intent is read-only.

### 7.4 Session-to-Config Boundary

Sessions receive a validated config object at construction time. Sessions do not modify config. Sessions may read config values during operation (e.g., connection parameters, retry limits). The config is immutable from the session's perspective.


## 8. Per-Session Status

### 8.1 MatrixSession

| Dimension | Status |
|-----------|--------|
| Lifecycle | Complete: start/stop/diagnostics |
| Reconnect | Bounded exponential backoff, max 10, cap 60s |
| E2EE | Implemented. Crypto store continuity across restarts. |
| Send retry | 3 attempts with jitter (adapter-level, not session) |
| Diagnostics | Frozen dataclass, 14 fields |
| Size | 682 LOC. Acceptable. |
| Split candidate | E2EE lifecycle extraction. Low priority. |
| Beta status | Most complete. Needs live inbound reception test. |

### 8.2 MeshtasticSession

| Dimension | Status |
|-----------|--------|
| Lifecycle | Complete: start/stop/diagnostics |
| Reconnect | Bounded exponential backoff, max 10, cap 30s |
| E2EE | Not applicable (AES-256 CTR at channel level, not session-managed) |
| Send retry | 3 attempts with jitter (session-level) |
| Diagnostics | Frozen dataclass, 8 fields |
| Size | 608 LOC. Acceptable. No split candidate. |
| Beta status | Needs confirmed delivery plumbing. `deliver()` returns `None`. |

### 8.3 MeshCoreSession

| Dimension | Status |
|-----------|--------|
| Lifecycle | Complete: start/stop/diagnostics |
| Reconnect | Bounded exponential backoff, max 10, cap 30s |
| E2EE | Not applicable |
| Send retry | 3 attempts with jitter (session-level) |
| Diagnostics | Plain dict copy, 5 fields |
| Size | 654 LOC. Acceptable. No split candidate. |
| Beta status | Needs confirmed delivery. BLE mode untested. |

### 8.4 LxmfSession

| Dimension | Status |
|-----------|--------|
| Lifecycle | Complete: start/stop/diagnostics |
| Reconnect | Bounded exponential backoff, max 10, cap 30s |
| E2EE | Built into Reticulum link layer, not session-managed |
| Send retry | 3 attempts with jitter (session-level) |
| Diagnostics | Frozen dataclass, 6 fields |
| Size | 1260 LOC. High complexity. |
| Split candidate | Delivery state tracker extraction. Not blocking. |
| Beta status | Needs live delivery state progression test. Identity file protection. |


## 9. Contractual Guarantees for Beta

1. **Sessions own SDK lifecycle end to end.** No other module touches the SDK client directly.
2. **Sessions never construct canonical events.** They forward normalized plain dicts to the adapter callback.
3. **Session diagnostics are read-only snapshots.** No mutable state is exposed.
4. **Session size will not grow beyond 1500 LOC** without a documented extraction plan.
5. **No new session responsibilities will be added** without updating this contract.
6. **Reconnect parameters are bounded.** Max 10 attempts, exponential backoff with jitter, cap 30s (60s for Matrix).
7. **Send retry is bounded.** Max 3 attempts per transport send call.
8. **The session-to-adapter boundary (plain dict callback) will not change** without a contract version bump.
