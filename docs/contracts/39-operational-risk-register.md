# Operational Risk Register

> Contract version: 1
> Last updated: 2026-05-10
> Track: Beta Operational Risk (Track 6)
> Supersedes: Nothing. Consolidates risk observations from contracts 33, 34, 35, 36, 37.
> Status: Risk register. Honest assessment of operational risks for medre beta.

This document is the operational risk register for medre. It catalogs risks
that affect the reliability, security, maintainability, and operational
usefulness of medre at the beta stage. Each risk is classified by category,
severity, likelihood, current mitigation, residual exposure, and ownership
boundary.

This register does not propose mitigations that require new features. It
records what is known and what is uncertain. Some risks are inherent to radio
protocols and cannot be mitigated within medre. Some risks are specific to
dependency choices. Some risks are operational unknowns that only live testing
can resolve.

Risk ratings use the following scale:

| Rating | Meaning |
|--------|---------|
| **Critical** | Will cause incorrect or unsafe behavior in normal operation. Must resolve before beta. |
| **High** | Likely to cause problems under realistic conditions. Should resolve or document before beta. |
| **Medium** | May cause problems under specific conditions. Document and monitor. |
| **Low** | Unlikely to cause problems in practice. Record for awareness. |


## 1. Transport Risks

### T1: MeshCore — no live validation

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | MeshCore adapter has never been run against real hardware. Unit tests pass against mocks. The adapter may have fundamental incompatibilities with real MeshCore radios. |
| **Severity** | High |
| **Likelihood** | Medium — adapter may work; the risk is that we do not know |
| **Mitigation** | Live harness exists (`test_meshcore_live.py`). Requires MeshCore radio hardware and environment variables. |
| **Residual exposure** | Full — no evidence either way |
| **Ownership** | medre owns the adapter. MeshCore SDK owns the radio protocol. medre cannot validate without hardware. |
| **Source** | Contract 37 §6, Contract 32 M14 |

### T2: LXMF — no live validation

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | LXMF adapter has never been run against a real Reticulum network. Delivery state progression model (`OUTBOUND → DELIVERED`) is implemented but unconfirmed. |
| **Severity** | High |
| **Likelihood** | Medium — most complex session (1,260 LOC); timing assumptions may break on real network |
| **Mitigation** | Live harness exists (`test_lxmf_live.py`, 829 LOC — most comprehensive harness). Requires Reticulum instance and identity file. |
| **Residual exposure** | Full — no evidence either way |
| **Ownership** | medre owns the adapter. Reticulum/LXMF owns the transport. |
| **Source** | Contract 37 §7, Contract 32 M15 |

### T3: Matrix — third-party inbound unconfirmed

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | No live test has confirmed that the Matrix adapter receives messages sent by a second Matrix account. Self-message suppression works, but that is not the same as third-party reception. |
| **Severity** | High |
| **Likelihood** | Low — inbound reception is core nio functionality; unlikely to be broken |
| **Mitigation** | Inbound reception test exists in live harness but has not been executed against real traffic. |
| **Residual exposure** | Medium — the code path exists but is unconfirmed end-to-end |
| **Ownership** | medre owns the adapter. nio owns the sync loop. |
| **Source** | Contract 32 M16 |

### T4: Meshtastic — fire-and-forget delivery uncertainty

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | `AdapterDeliveryResult.success=True` means local radio accepted the packet. No end-to-end delivery confirmation exists in the Meshtastic protocol for text messages. Messages may be silently dropped after local handoff. |
| **Severity** | Medium |
| **Likelihood** | High — this is inherent to the protocol, not a bug |
| **Mitigation** | Documented in contract 36. Consumer must treat radio delivery as best-effort. |
| **Residual exposure** | Full — cannot be mitigated within medre |
| **Ownership** | Meshtastic protocol. medre reports what it knows (local handoff). Consumer owns deduplication and retry. |
| **Source** | Contract 36 §2.1 |

### T5: MeshCore — fire-and-forget delivery uncertainty

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | Same as T4 but for MeshCore. Local radio confirms link-level send. No end-to-end delivery confirmation. |
| **Severity** | Medium |
| **Likelihood** | High — inherent to radio protocol |
| **Mitigation** | Documented in contract 36. |
| **Residual exposure** | Full — cannot be mitigated within medre |
| **Ownership** | MeshCore protocol. Consumer owns deduplication and retry. |
| **Source** | Contract 36 §2.2 |

### T6: LXMF — delivery state uncertainty

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | LXMF delivery states (`OUTBOUND → SENDING → SENT → DELIVERED`) are tracked in the session but not observed against a real network. State transitions may have timing assumptions that break under real Reticulum latency (seconds to hours). Propagated messages may wait at a propagation node indefinitely. |
| **Severity** | Medium |
| **Likelihood** | Medium — state model is the most ambitious of all radio transports |
| **Mitigation** | Live harness includes delivery state tests. Not yet run. |
| **Residual exposure** | Full until live validation |
| **Ownership** | medre owns the state model. LXMRouter owns the actual delivery. medre does not currently surface delivery state progression to the consumer. |
| **Source** | Contract 36 §2.3, Contract 37 §7 |

### T7: BLE mode untested (Meshtastic, MeshCore)

| Field | Value |
|-------|-------|
| **Category** | Transport |
| **Risk** | BLE connection constructors exist in both Meshtastic and MeshCore adapters but have never been exercised. BLE has different connection semantics, power management, and disconnection behavior compared to serial/TCP. |
| **Severity** | Medium |
| **Likelihood** | Medium — BLE is a common connection mode for radio hardware |
| **Mitigation** | Constructors exist. Not tested. |
| **Residual exposure** | Full for BLE users |
| **Ownership** | medre owns the adapter. SDK owns BLE stack. |
| **Source** | Contract 37 §5.3, §6.3 |


## 2. Dependency Risks

### D1: mindroom-nio fork maintenance

| Field | Value |
|-------|-------|
| **Category** | Dependency |
| **Risk** | `mindroom-nio` is a maintained fork of `matrix-nio`. The project must track upstream for security patches, API changes, and bug fixes. If upstream makes breaking changes and the fork does not follow, the Matrix adapter breaks. |
| **Severity** | High |
| **Likelihood** | Low — fork is currently maintained |
| **Mitigation** | Version pinned to `>=0.25.3`. Dependency documented in pyproject.toml comments and contract 34. |
| **Residual exposure** | Ongoing maintenance burden. Fork could become unmaintained. |
| **Ownership** | Project owns the fork decision. Upstream `matrix-nio` owns the base library. |
| **Source** | Contract 34 §4.1, Contract 37 §4.3 |

### D2: mtjk fork maintenance

| Field | Value |
|-------|-------|
| **Category** | Dependency |
| **Risk** | `mtjk` is a maintained fork of the Meshtastic Python library. Same fork maintenance risk as D1. Additionally, the distribution name (`mtjk`) differs from the import name (`meshtastic`), which can confuse debugging. |
| **Severity** | Medium |
| **Likelihood** | Low — fork is currently maintained |
| **Mitigation** | Version pinned to `>=2.7.8`. Documented in pyproject.toml. |
| **Residual exposure** | Ongoing maintenance burden. |
| **Ownership** | Project owns the fork decision. Upstream Meshtastic Python owns the base library. |
| **Source** | Contract 34 §4.2 |

### D3: meshcore_py SDK maturity

| Field | Value |
|-------|-------|
| **Category** | Dependency |
| **Risk** | `meshcore_py` (v2.2.5–2.3.7) is a small-community SDK. API stability is not guaranteed. Breaking changes may occur between minor versions. |
| **Severity** | Medium |
| **Likelihood** | Medium — small community increases risk of unannounced breaking changes |
| **Mitigation** | Version pinned to `>=2.3.7`. Small API surface used by medre. |
| **Residual exposure** | Medium — SDK is not widely battle-tested |
| **Ownership** | meshcore_py authors own the SDK. medre owns the adapter layer. |
| **Source** | Contract 34 §4.5, Contract 37 §6.3 |

### D4: vodozemac (Rust) install friction

| Field | Value |
|-------|-------|
| **Category** | Dependency |
| **Risk** | Matrix E2EE requires `vodozemac`, a Rust crate. Binary wheels exist for common platforms (Linux x86_64, macOS, Windows) but not all. Alpine and ARM may require a Rust toolchain, adding install complexity. |
| **Severity** | Medium |
| **Likelihood** | Low — most users are on platforms with binary wheels |
| **Mitigation** | E2EE is optional (`.[matrix-e2e]`). Plaintext Matrix (`.[matrix]`) has no Rust dependency. Documented in contract 34. |
| **Residual exposure** | Users on non-standard platforms cannot use E2EE without a Rust toolchain. |
| **Ownership** | vodozemac authors own the wheel distribution. medre documents the requirement. |
| **Source** | Contract 34 §4.1, Contract 25 §1.2 |

### D5: Reticulum non-standard license

| Field | Value |
|-------|-------|
| **Category** | Dependency |
| **Risk** | Reticulum (required by LXMF) uses a non-OSI-approved license (Reticulum License). This may affect downstream distribution, commercial use, or inclusion in package indices. |
| **Severity** | Low |
| **Likelihood** | Low — most beta users are developers evaluating the framework |
| **Mitigation** | Documented in contract 34. LXMF is an optional dependency. |
| **Residual exposure** | Downstream consumers must review the license themselves. |
| **Ownership** | Reticulum authors own the license. medre documents the dependency. |
| **Source** | Contract 34 §4.6 |


## 3. Operational Risks

### O1: No sustained throughput evidence

| Field | Value |
|-------|-------|
| **Category** | Operational |
| **Risk** | No transport has been tested under sustained load. Throughput limits, memory growth under load, and queue behavior at capacity are unknown. |
| **Severity** | Medium |
| **Likelihood** | Medium — will matter for any real workload |
| **Mitigation** | Live tests are smoke tests (single messages). Sustained testing deferred (contract 32 S2). |
| **Residual exposure** | Full — no data |
| **Ownership** | medre owns the pipeline. Consumer owns load testing for their use case. |
| **Source** | Contract 32 S2 |

### O2: No reconnect resilience evidence

| Field | Value |
|-------|-------|
| **Category** | Operational |
| **Risk** | No live test exercises adapter behavior during real network failures. Reconnect logic exists in sessions but has only been tested against mocks. |
| **Severity** | Medium |
| **Likelihood** | High — network failures are normal in production |
| **Mitigation** | Reconnect logic is bounded (max 10 attempts for Matrix, 3 for others). `_stop_requested` guard prevents reconnect loops. |
| **Residual exposure** | Medium — logic exists but unvalidated |
| **Ownership** | medre owns reconnect logic. Network conditions are outside medre's control. |
| **Source** | Contract 32 S1, Contract 35 |

### O3: Access token storage (Matrix)

| Field | Value |
|-------|-------|
| **Category** | Operational |
| **Risk** | Matrix access tokens are stored as plain strings in `MatrixConfig`. No rotation, refresh, or secure storage mechanism. `__repr__` redacts the token, but the value is accessible in memory and config files. |
| **Severity** | Medium |
| **Likelihood** | Low — standard for many Matrix bots; token rotation is uncommon |
| **Mitigation** | Environment variable injection recommended. `__repr__` redaction prevents logging leaks. Documented in `docs/runbooks/secure-credentials.md`. |
| **Residual exposure** | Operator must manage token security. No automated protection. |
| **Ownership** | Operator owns credential management. medre provides redaction. |
| **Source** | Contract 37 §4.3, Contract 32 S7 |

### O4: LXMF identity file security

| Field | Value |
|-------|-------|
| **Category** | Operational |
| **Risk** | Reticulum identity is stored as a 64-byte raw private key file with no encryption, no header, and no access protection beyond OS file permissions. Anyone with the file can impersonate the identity. |
| **Severity** | High |
| **Likelihood** | Low — requires file access, which is an OS-level concern |
| **Mitigation** | File permission requirements documented. Tests never log file contents. |
| **Residual exposure** | Full — if file is exposed, identity is compromised |
| **Ownership** | Operator owns file permissions. Reticulum owns the identity format. medre documents the risk. |
| **Source** | Contract 37 §7.3, Contract 32 S11 |

### O5: Diagnostics are not authoritative state

| Field | Value |
|-------|-------|
| **Category** | Operational |
| **Risk** | Consumers may treat `diagnostics()` output as authoritative, real-time state. Diagnostics are read-only snapshots at a point in time. They may be stale by the time the consumer reads them. |
| **Severity** | Low |
| **Likelihood** | Medium — temptation to use diagnostics for routing decisions |
| **Mitigation** | Documentation states diagnostics are observations, not state. Contract 29 defines the contract. |
| **Residual exposure** | Low — if documented clearly |
| **Ownership** | medre owns the diagnostics contract. Consumer owns interpretation. |
| **Source** | Contract 29 |


## 4. Hardware Risks

### H1: Radio hardware required for validation

| Field | Value |
|-------|-------|
| **Category** | Hardware |
| **Risk** | Meshtastic, MeshCore, and LXMF validation requires physical radio hardware or networked radio nodes. This hardware is not universally available and cannot be automated in CI. |
| **Severity** | High |
| **Likelihood** | High — this is a current blocker for MeshCore and LXMF beta-readiness |
| **Mitigation** | Live harnesses exist with `@require_live` skip guards. Unit tests cover all logic paths. |
| **Residual exposure** | Transports without live evidence carry full validation risk. |
| **Ownership** | Operator/developer owns hardware availability. medre provides the harness. |
| **Source** | Contract 37 §6, §7 |

### H2: Firmware version sensitivity (Meshtastic)

| Field | Value |
|-------|-------|
| **Category** | Hardware |
| **Risk** | The `mtjk` library assumes a specific protobuf schema tied to Meshtastic firmware. Firmware version mismatches may cause deserialization errors or silent data corruption. |
| **Severity** | Medium |
| **Likelihood** | Medium — firmware updates are common in the Meshtastic ecosystem |
| **Mitigation** | Live test records firmware version (2.7.19). Document version in runbook. |
| **Residual exposure** | Medium — firmware is outside medre's control |
| **Ownership** | Meshtastic firmware authors own the protobuf schema. medre documents the tested version. |
| **Source** | Contract 37 §5.3 |

### H3: Serial port permissions (Linux)

| Field | Value |
|-------|-------|
| **Category** | Hardware |
| **Risk** | On Linux, serial port access requires `dialout` group membership or udev rules. Docker requires `--device` passthrough. Users may encounter permission errors that appear to be adapter bugs. |
| **Severity** | Low |
| **Likelihood** | Medium — common friction point for new users |
| **Mitigation** | Documented in developer-environment.md and meshtastic-alpha-operation.md. |
| **Residual exposure** | Low — well-documented OS-level concern |
| **Ownership** | Operator owns OS configuration. medre documents the requirement. |
| **Source** | Contract 34 §4.2 |


## 5. E2EE Risks

### E1: No cross-signed device trust (Matrix)

| Field | Value |
|-------|-------|
| **Category** | E2EE |
| **Risk** | Matrix E2EE operates with `ignore_unverified_devices=True`. Without cross-signing support, strict device verification would block all encrypted sends. This means medre will encrypt to any device that appears in the room, including potentially compromised devices. |
| **Severity** | Medium |
| **Likelihood** | Low — requires a compromised device in an encrypted room |
| **Mitigation** | Documented as deliberate trade-off in contract 25. The alternative (strict verification) would make E2EE unusable for automated agents. |
| **Residual exposure** | Medium — accepted trade-off for usability |
| **Ownership** | medre owns the adapter configuration. nio owns the crypto implementation. Operator owns device verification policy. |
| **Source** | Contract 25 §5.2, Contract 37 §4.3 |

### E2: Crypto store continuity (Matrix)

| Field | Value |
|-------|-------|
| **Category** | E2EE |
| **Risk** | E2EE depends on a persistent crypto store (SQLite via peewee). If the store is deleted, corrupted, or becomes inaccessible, the device can no longer decrypt messages encrypted to the previous keys. Historical messages become permanently unreadable. |
| **Severity** | Medium |
| **Likelihood** | Low — requires operator action or disk failure |
| **Mitigation** | `restore_login` maintains store continuity across restarts. Store path is configurable. |
| **Residual exposure** | Low — normal operational hygiene |
| **Ownership** | Operator owns the store file. nio owns the store format. |
| **Source** | Contract 25 |

### E3: E2EE scope limited to Matrix text

| Field | Value |
|-------|-------|
| **Category** | E2EE |
| **Risk** | E2EE exists only for Matrix text messages in encrypted rooms. No other transport has application-level E2EE managed by medre. Users may assume radio transports have E2EE because the protocols advertise encryption. |
| **Severity** | Low |
| **Likelihood** | Medium — possible user confusion |
| **Mitigation** | README and contract 25 clearly state scope. Radio transport E2EE is at the protocol level, not managed by medre. |
| **Residual exposure** | Low — documented |
| **Ownership** | medre owns E2EE for Matrix. Radio protocols own their own encryption. User owns understanding of the difference. |
| **Source** | Contract 25, Contract 36 |


## 6. Reconnect Risks

### R1: Sync task leak on stop timeout (Matrix)

| Field | Value |
|-------|-------|
| **Category** | Reconnect |
| **Risk** | If `stop()` times out waiting for the sync task, the task reference is cleared but the coroutine may still be running. The nio client may hold open HTTP connections. |
| **Severity** | Low |
| **Likelihood** | Low — requires specific timing during shutdown |
| **Mitigation** | `_stop_requested` flag prevents reconnect loops even if task lingers. Default timeout is 5 seconds. |
| **Residual exposure** | Low — task will eventually terminate |
| **Ownership** | medre owns the stop logic. nio owns the HTTP connection lifecycle. |
| **Source** | Contract 35 §3.1 |

### R2: Reconnect without live validation

| Field | Value |
|-------|-------|
| **Category** | Reconnect |
| **Risk** | All reconnect logic is tested against mocks. No live test exercises a real disconnection and reconnection cycle. Reconnect may have timing or state assumptions that break with real network behavior. |
| **Severity** | Medium |
| **Likelihood** | Medium — reconnect code is exercised in unit tests but not under real failure |
| **Mitigation** | Bounded retry budgets (3–10 attempts). `_stop_requested` guard. |
| **Residual exposure** | Medium until live failure testing |
| **Ownership** | medre owns reconnect logic. |
| **Source** | Contract 32 S1, Contract 35 |


## 7. Delivery Uncertainty Risks

### U1: Duplicate messages from retries

| Field | Value |
|-------|-------|
| **Category** | Delivery uncertainty |
| **Risk** | Meshtastic and MeshCore sessions retry transient failures up to 3 times. If the first attempt succeeds at the radio level but the response is lost, the retry produces a duplicate. Consumers must handle deduplication. |
| **Severity** | Medium |
| **Likelihood** | Medium — normal under radio interference or marginal conditions |
| **Mitigation** | Documented in contract 33 and contract 36. `AdapterDeliveryResult.attempts` records retry count. |
| **Residual exposure** | Full — deduplication is consumer's responsibility |
| **Ownership** | medre reports attempts. Consumer deduplicates. |
| **Source** | Contract 33 §3.3, Contract 36 |

### U2: Multi-hop delivery latency (radio transports)

| Field | Value |
|-------|-------|
| **Category** | Delivery uncertainty |
| **Risk** | Radio transports (Meshtastic, MeshCore, LXMF) may route messages through multiple hops. Delivery latency ranges from seconds to hours. Consumers may time out before delivery completes. |
| **Severity** | Medium |
| **Likelihood** | High — multi-hop is normal for radio mesh |
| **Mitigation** | Fire-and-forget model documented in contract 36. medre does not wait for remote confirmation. |
| **Residual exposure** | Full — inherent to radio protocols |
| **Ownership** | Radio protocols. medre reports local handoff. Consumer owns timeout policy. |
| **Source** | Contract 36 |

### U3: LXMF propagated message indefinite delay

| Field | Value |
|-------|-------|
| **Category** | Delivery uncertainty |
| **Risk** | LXMF propagated messages wait at a propagation node until the recipient connects. If the recipient never connects, the message is never delivered. There is no timeout or bounce mechanism visible to medre. |
| **Severity** | Medium |
| **Likelihood** | Low — depends on recipient behavior |
| **Mitigation** | Not currently observed by medre. Delivery state tracking exists but is unvalidated. |
| **Residual exposure** | Full until live validation |
| **Ownership** | LXMRouter owns delivery. medre owns the adapter. |
| **Source** | Contract 36 §2.3 |


## 8. Maintenance Risks

### M1: Fork tracking burden

| Field | Value |
|-------|-------|
| **Category** | Maintenance |
| **Risk** | Two dependencies are project-maintained forks (`mindroom-nio`, `mtjk`). Each fork requires ongoing tracking of upstream changes, security patches, and API drift. Fork maintenance is a recurring cost. |
| **Severity** | Medium |
| **Likelihood** | Medium — upstream activity is ongoing |
| **Mitigation** | Version pins in pyproject.toml. Fork rationale documented in contract 34. |
| **Residual exposure** | Ongoing — forks must be maintained as long as they are used |
| **Ownership** | Project owns the fork maintenance decision. |
| **Source** | Contract 34 §4.1, §4.2 |

### M2: SDK API instability (MeshCore)

| Field | Value |
|-------|-------|
| **Category** | Maintenance |
| **Risk** | `meshcore_py` is a small-community SDK that may change APIs between versions. medre's adapter may break on SDK updates. |
| **Severity** | Medium |
| **Likelihood** | Medium — small community, active development |
| **Mitigation** | Version pinned. Adapter uses a small API surface. |
| **Residual exposure** | Medium — adapter must be updated when SDK changes |
| **Ownership** | meshcore_py authors own the SDK. medre owns the adapter. |
| **Source** | Contract 34 §4.5 |

### M3: Test coverage gaps in newer transports

| Field | Value |
|-------|-------|
| **Category** | Maintenance |
| **Risk** | MeshCore has the lowest session test count (18 functions vs. 102 for Matrix). Edge cases in session lifecycle may be undertested. |
| **Severity** | Low |
| **Likelihood** | Low — core paths are covered |
| **Mitigation** | Identified in contract 37. Target is 40+ session tests. |
| **Residual exposure** | Low — edge case gaps, not core path gaps |
| **Ownership** | medre owns the test suite. |
| **Source** | Contract 37 §6.2 |


## 9. Upstream Risks

### UP1: Meshtastic protobuf schema changes

| Field | Value |
|-------|-------|
| **Category** | Upstream |
| **Risk** | Meshtastic firmware updates may change the protobuf schema, causing deserialization failures in the `mtjk` library. This is outside medre's control. |
| **Severity** | Medium |
| **Likelihood** | Low — protobuf changes are typically backward-compatible |
| **Mitigation** | Document tested firmware version in runbook. Pin SDK version. |
| **Residual exposure** | Medium — firmware is user-controlled |
| **Ownership** | Meshtastic project owns the protobuf schema. mtjk fork tracks it. medre documents the tested version. |
| **Source** | Contract 37 §5.3 |

### UP2: Matrix spec changes

| Field | Value |
|-------|-------|
| **Category** | Upstream |
| **Risk** | Matrix specification changes may affect event formats, room versions, or encryption mechanisms. nio (and the mindroom-nio fork) must track these changes. |
| **Severity** | Low |
| **Likelihood** | Low — Matrix spec evolution is slow and backward-compatible |
| **Mitigation** | Version pinned. mindroom-nio fork tracks upstream. |
| **Residual exposure** | Low — normal upstream tracking |
| **Ownership** | Matrix.org owns the spec. mindroom-nio fork implements it. medre uses the SDK. |
| **Source** | Contract 34 §4.1 |

### UP3: Reticulum daemon stability

| Field | Value |
|-------|-------|
| **Category** | Upstream |
| **Risk** | LXMF depends on Reticulum, which is designed for long-running daemons. Short-lived processes may not establish stable mesh connectivity. medre's session lifecycle (start → operate → stop) may conflict with Reticulum's daemon-oriented design. |
| **Severity** | Medium |
| **Likelihood** | Medium — depends on usage pattern |
| **Mitigation** | LXMF session manages LXMRouter lifecycle. Not validated against real network. |
| **Residual exposure** | Full until live validation |
| **Ownership** | Reticulum authors own the daemon design. medre owns the session lifecycle. |
| **Source** | Contract 37 §7.3 |


## 10. Risk Summary

| ID | Category | Risk | Severity | Likelihood | Mitigated? |
|----|----------|------|----------|------------|------------|
| T1 | Transport | MeshCore no live validation | High | Medium | No |
| T2 | Transport | LXMF no live validation | High | Medium | No |
| T3 | Transport | Matrix third-party inbound unconfirmed | High | Low | No |
| T4 | Transport | Meshtastic fire-and-forget | Medium | High | Documented |
| T5 | Transport | MeshCore fire-and-forget | Medium | High | Documented |
| T6 | Transport | LXMF delivery state uncertainty | Medium | Medium | No |
| T7 | Transport | BLE mode untested | Medium | Medium | No |
| D1 | Dependency | mindroom-nio fork maintenance | High | Low | Pinned |
| D2 | Dependency | mtjk fork maintenance | Medium | Low | Pinned |
| D3 | Dependency | meshcore_py SDK maturity | Medium | Medium | Pinned |
| D4 | Dependency | vodozemac Rust install friction | Medium | Low | Optional |
| D5 | Dependency | Reticulum non-standard license | Low | Low | Documented |
| O1 | Operational | No sustained throughput evidence | Medium | Medium | Deferred |
| O2 | Operational | No reconnect resilience evidence | Medium | High | Deferred |
| O3 | Operational | Matrix access token storage | Medium | Low | Documented |
| O4 | Operational | LXMF identity file security | High | Low | Documented |
| O5 | Operational | Diagnostics misinterpretation | Low | Medium | Documented |
| H1 | Hardware | Radio hardware required | High | High | Harness exists |
| H2 | Hardware | Firmware version sensitivity | Medium | Medium | Documented |
| H3 | Hardware | Serial port permissions | Low | Medium | Documented |
| E1 | E2EE | No cross-signed device trust | Medium | Low | Documented |
| E2 | E2EE | Crypto store continuity | Medium | Low | Documented |
| E3 | E2EE | E2EE scope confusion | Low | Medium | Documented |
| R1 | Reconnect | Sync task leak on stop timeout | Low | Low | Guarded |
| R2 | Reconnect | Reconnect without live validation | Medium | Medium | Bounded |
| U1 | Delivery | Duplicate messages from retries | Medium | Medium | Documented |
| U2 | Delivery | Multi-hop delivery latency | Medium | High | Documented |
| U3 | Delivery | LXMF propagated message delay | Medium | Low | Unvalidated |
| M1 | Maintenance | Fork tracking burden | Medium | Medium | Pinned |
| M2 | Maintenance | MeshCore SDK API instability | Medium | Medium | Pinned |
| M3 | Maintenance | Test coverage gaps (MeshCore) | Low | Low | Identified |
| UP1 | Upstream | Meshtastic protobuf changes | Medium | Low | Pinned |
| UP2 | Upstream | Matrix spec changes | Low | Low | Pinned |
| UP3 | Upstream | Reticulum daemon stability | Medium | Medium | Unvalidated |

**Unmitigated high-severity risks: 4** (T1, T2, T3, H1)

All four share the same root cause: lack of live validation evidence. The
primary risk reduction action is running live harnesses against real endpoints
and recording the results.

**Inherent (unfixable) risks: 3** (T4, T5, U2)

These are properties of radio protocols, not defects. They are documented,
accepted, and will never be "resolved." Consumer education is the only
mitigation.
