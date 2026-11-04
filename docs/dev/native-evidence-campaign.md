# Native evidence campaign

This page is the working inventory of native-evidence legs for the open
register gaps in [contract-gap-status.md](contract-gap-status.md): G01
(exact-destination replay selection and decision), G02 (uncertain
native-handoff remediation), and G05 (recipient and native verification
on the current pin set). It maps each leg to the strongest evidence tier
it can reach today, names the harness that carries it, and records which
legs are CI-automatable now, which need a local service tier, and which
stay operator-run evidence because hardware or an external peer is the
only honest source. It is a status inventory, not a schedule.

## Scope and evidence rules

Two properties are in scope for every transport:

- **Exact-target native acceptance (G01/G02).** A replay or decision
  authority-authorized send is observed accepted by the real provider,
  independently of MEDRE's own receipts. The qualification boundary for
  the exact-target property is set by
  [exact-target-recovery.md](../spec/exact-target-recovery.md): the fake
  tier proves transactional properties only, and a fake-tier handoff is
  never read as provider acceptance.
- **Recipient and native verification on the current pins (G05).** The
  per-claim list from the register: encrypted Matrix, radio
  interruption, LXMF propagation, SimpleX replies, XMPP, and Chatmail.

Evidence tiers follow [transport-realism.md](../spec/appendices/transport-realism.md):
`fake_pipeline`, `sdk_contract`, `local_integration`, `docker` (a
container-backed local service), `live` (an external service), and
`hardware`. A lower tier never stands in for a higher one; a local
integration using an in-process endpoint remains conformance evidence,
while a container-backed local service is `docker` evidence. Native
handoff is not recipient receipt, and an adapter receipt never substitutes
for an independent observation of the provider or the recipient.

The checked basis for every leg is the seven transport pins recorded in
`.github/adapter-compatibility.json` — that manifest is the pin
authority. Provider-owned harnesses are named as repository-relative
paths at the pinned commits; core-owned harnesses are `tests/...`
paths. SDK pins are declared by each provider's own package metadata,
so this page names SDK packages rather than versions. Claims that cannot
be verified from the pinned provider repositories alone say so
explicitly.

The typed vocabulary these observations project into — bounded result,
scope, tier, and the authority rules binding them to core attempt
identity — is specified in
[native-evidence.md](../spec/native-evidence.md) and implemented by
`src/medre/core/evidence/native_evidence.py`. This page remains the
per-transport evidence inventory; nothing on it changes meaning.

## Tier map

| Tier label        | Meaning for this campaign                                            |
| ----------------- | -------------------------------------------------------------------- |
| `fake_pipeline`   | Core seams with a synthetic adapter boundary (already landed for the exact-target contract; see the register's stage detail). |
| `sdk_contract`    | The exact pinned SDK installed and its surface frozen by contract tests. |
| `local_integration` | MEDRE plus the exact SDK against a deterministic local endpoint or an in-process peer. |
| `docker`          | A containerized local service tier executed by `tests/integration/` and the Docker integration workflow. |
| `live`            | An external service or an independent recipient/peer; operator-run by design. |
| `hardware`        | Physical radios and devices; operator-run evidence, opt-in only.      |

CI-automatable today means the harness runs in the default gate or the
Docker/local-integration workflows: `tests/integration/` Docker tests run
through `scripts/ci/run-docker-integration.sh` in the Docker integration
workflow, `local_integration` provider suites run in the
transport-local-integration job when the manifest marks the transport
`local_integration: true` (MeshCore and LXMF), and the core-owned LXMF
exact-target propagation scenario runs in the required
`core-owned-lxmf-integration` job, which installs the pinned lxmf adapter
distribution and its exact-pinned RNS/LXMF SDKs and fails when the
scenario is not collected or is skipped.

## Matrix

Pinned provider: `medre-adapter-matrix`; SDK package `mindroom-nio[e2e]`
per the provider's package metadata.

| Leg                                        | Highest tier reached today | Basis                                                                                                                                                                                                        | Automation                        |
| ------------------------------------------ | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------- |
| Exact-target native acceptance (G01/G02)   | `docker`                   | `tests/integration/test_synapse_exact_target_acceptance.py`: scoped replay sends through the real adapter to the containerized Synapse homeserver, and acceptance is observed through a second Matrix client reading the provider timeline. The typed provider-acceptance observation is checked against the exact durable outbox generation and immutable attempt receipt, including historical-versus-current classification after replay; a reused native event ID leaves the new replay attempt unresolved rather than claiming a new acceptance, and the ledger check does not authenticate the observer. | CI-automatable now (Docker integration workflow) |
| Encrypted Matrix verification (G05)        | `docker` (partial, inbound only) | `tests/integration/test_synapse_e2ee_smoke.py` drives genuine Megolm encryption client-side through a second E2EE-capable nio client and the adapter's crypto stack; no leg yet observes an authority-authorized encrypted outbound send from the provider side. | Harness exists; leg outstanding on the same Docker tier |
| Encrypted continuity under a live account (G05) | `live`                | Provider-owned `tests/test_matrix_e2ee_live.py` and `tests/test_matrix_durable_runtime_live.py` at the pinned provider commit, both live-marked.                                                             | Operator-run (external homeserver) |

The Docker Synapse tier pins its image digest in
`docker-compose.integration.yaml` (kept in sync by
`tests/test_integration_image_alignment.py`); this page deliberately does
not repeat the tag. The provider-side idempotency behavior that the
acceptance leg observes — the pinned adapter sends with deterministic
transaction ids, so a replay of an already-accepted target may collapse
into the original event — is recorded by the test itself; generalizing
that behavior across homeservers cannot be verified from the pinned
provider repositories alone and is not claimed here. When replay returns
an already-accepted event identity, the independent timeline confirms that
identity's prior acceptance; it does not prove a new provider request or a
second native delivery.

## Meshtastic

Pinned provider: `medre-adapter-meshtastic`; SDK package `mtjk`.

| Leg                                      | Highest tier reached today | Basis                                                                                                                                              | Automation                                     |
| ---------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| Exact-target native acceptance (G01/G02) | `docker`                   | `tests/integration/test_meshtasticd_sdk_bridge.py` proves outbound SDK handoff through containerized `meshtasticd`; a scoped replay send over that tier is the reachable acceptance evidence, because the supported send path requests no acknowledgements (see the transport limitations appendix, §2.2). | Harness exists; the exact-target leg over `meshtasticd` is outstanding |
| Radio interruption (G05)                 | `hardware`                 | Provider-owned `tests/test_meshtastic_pair_live.py` and `tests/test_meshtastic_hardware_soak.py` at the pinned provider commit, hardware/live-marked; recorded radio observations are dated evidence, not current-pin qualification. | Operator-run (physical radios; never transmits from the default suite) |

Acceptance at the `meshtasticd` tier proves the local node accepted the
packet, not mesh arrival: no observable routing result qualifies as
remote or end-to-end delivery evidence at this pin.

## MeshCore

Pinned provider: `medre-adapter-meshcore`; SDK package `meshcore`.

| Leg                                      | Highest tier reached today | Basis                                                                                                                                                                                                                       | Automation                                       |
| ---------------------------------------- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| Exact-target native acceptance (G01/G02) | `local_integration`        | Provider-owned `tests/test_meshcore_local_integration.py` drives the real SDK through a deterministic TCP companion-protocol endpoint (marked `local_integration` + `meshcore_sdk`; the manifest marks the transport `local_integration: true`). | Required transport-local-integration job also runs the core-owned exact-target companion suite; physical radio/recipient proof remains outstanding |
| Radio interruption / BLE hardware (G05)  | `hardware`                 | Provider-owned `tests/test_meshcore_live.py` at the pinned provider commit; BLE session-layer evidence is a dated live observation (see the known-limitations index), and serial transport has no non-hardware coverage.       | Operator-run (pre-paired BLE node or serial radio) |

The core-owned `tests/native_qualification/test_meshcore_exact_target_replay.py` now
exercises core replay against this same pinned companion endpoint. It covers
single/multipart selected-channel commands, excluded siblings, invalid targets,
stale-preview generation fencing, reconnect, and single/multipart uncertainty
before/after an accepted prefix across restart. The required MeshCore
local-integration job enforces all 15 cases and their positive proof records.
The local SDK run is protocol conformance, not physical companion or recipient
RF proof; channel `OK` does not assign a native message ID. Core-rendered native
single packets now use the same durable grant. Direct unbound session retries
remain outside the core-owned no-resubmission claim.

MeshCore has no store-and-forward and no native reply mechanism at this
pin, so recipient-side verification beyond radio-level interruption
remains a hardware-tier property.

## LXMF

Pinned provider: `medre-adapter-lxmf`; SDK packages `lxmf` and `rns`.

| Leg                                                    | Highest tier reached today | Basis                                                                                                                                                                                                                                                                                                       | Automation                                                                                                                                |
| ------------------------------------------------------ | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- |
| LXMF local propagation and recipient observation (G05) | `local_integration`        | Core-owned `tests/integration/test_lxmf_exact_target_propagation.py`: two process-isolated Reticulum instances over a loopback-only UDPInterface pair, the recipient being a real propagation node observed through its own delivery callback in its own process (marked `local_integration` + `lxmf_sdk`). | Collected and executed by the required `core-owned-lxmf-integration` job at the pinned SDKs, which fails on not-collected or skipped runs |
| Core-authorized exact-target LXMF replay (G01/G02) | `local_integration` | New gated `tests/integration/test_lxmf_core_exact_target_replay.py` launches the real core `ReplayRequest` through `PipelineRunner` and the pinned `LxmfAdapter` worker, checks one durable generation and receipt, and compares the native hash to an independent recipient callback. The test binds its typed observation back to that exact attempt. Executed successfully at the manifest pin with independent recipient evidence; it does not exercise uncertain-handoff remediation. | Executed by the opt-in `lxmf-core-native-qualification.yml` at the manifest pin; opt-in, not required CI, and not a closed contract claim |
| LXMF local router acceptance (G05)                     | `local_integration`        | Provider-owned `tests/test_lxmf_local_integration.py` runs a process-isolated real Reticulum/LXMRouter lifecycle (marked `local_integration`; the manifest marks the transport `local_integration: true`). Acceptance at this tier is local router acceptance inside the worker process.                    | CI-automatable now (transport-local-integration job); the exact-target leg over that provider-owned harness is outstanding                |
| LXMF propagation over an external peer (G05)           | `live`                     | Provider-owned `tests/test_lxmf_live.py` at the pinned provider commit; propagated sends need an explicit outbound propagation node and an external peer. Multi-hop mesh delivery is not tested at this pin.                                                                                                | Operator-run (external Reticulum peer or propagation node)                                                                                |
| Physical-interface lifecycle                           | `hardware`                 | Provider-owned `tests/test_native_worker_hardware.py` opens workers against a private RNode profile and establishes no delivery or reachability.                                                                                                                                                            | Operator-run (RNode hardware)                                                                                                             |

The existing executed local SDK propagation scenario exercises native session
and recipient behavior, not MEDRE's core replay or audited decision authority.
The additional core-to-provider scenario has executed successfully at its
manifest pin through the opt-in qualification workflow, which installs the
pinned Matrix monitor beside the LXMF provider so the cross-provider
non-delivery assertion holds in the runner environment. The independent
recipient observed exactly one delivery. The earlier stamp-generation stall
was an adapter worker defect — the worker's protocol reader held a lock that
SDK fork children must take — resolved by reading protocol input from a
duplicate of file descriptor 0. Local replay acceptance and
uncertain-handoff remediation are separate contracts; a successful replay
does not permit another attempt after uncertainty.

Uncertain native handoff after the router call is fenced by the
provider's handoff journal at this pin (see the transport limitations
appendix, §2.4); the exact-target acceptance legs over the loopback
harnesses therefore prove local-tier acceptance only, and never network
arrival. The core-owned scenario's propagated delivery terminates at the
same loopback instance that runs the propagation node, so it stays a
local-tier claim: no multi-hop arrival, propagation peering, sync flow,
external network, or hardware evidence is claimed for it.

## XMPP

Pinned provider: `medre-adapter-xmpp`; SDK package `slixmpp`.

| Leg                                      | Highest tier reached today | Basis                                                                                                   | Automation                                            |
| ---------------------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Exact-target native acceptance (G01/G02) | `sdk_contract`             | Provider-owned `tests/test_sdk_contract.py` freezes the pinned slixmpp surface; outbound success at this pin means the stanza was written to the verified TLS stream. | SDK contract tier; a containerized local XMPP server tier is not built yet and would be the next reachable step |
| XMPP recipient verification (G05)        | `live`                     | Provider-owned `tests/test_live.py` at the pinned provider commit, live-marked.                          | Operator-run (external server and independent peer)   |

No core or provider harness currently observes server-side acceptance
of a stanza (for example a MUC echo from a containerized server);
whether that observation is reachable through the pinned SDK cannot be
verified from the pinned provider repositories alone, and is not
claimed here.

## Chatmail

Pinned provider: `medre-adapter-chatmail`; SDK packages
`deltachat-rpc-client` and `deltachat-rpc-server`.

| Leg                                      | Highest tier reached today | Basis                                                                                                                                                          | Automation                                          |
| ---------------------------------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| Exact-target native acceptance (G01/G02) | `sdk_contract`             | Provider-owned `tests/test_sdk_contract.py` freezes the pinned RPC surface; outbound success at this pin means admission to the provider's local queue, with sent state reported by the native core after SMTP acceptance. | SDK contract tier; a containerized Chatmail relay tier is not built yet and would be the next reachable step |
| Chatmail peer verification (G05)         | `live`                     | Provider-owned `tests/test_live.py` at the pinned provider commit, live-marked; the peer-evidence entry in the known-limitations index carries a recorded evidence age. | Operator-run (external relay and independent peer)  |

Because the native core owns SMTP/IMAP, an acceptance observation above
the local queue tier depends on what the pinned native core reports;
nothing in the pinned provider repositories alone establishes a
server-side delivery observation for a containerized relay, and no such
claim is made here.

## SimpleX Chat

Pinned provider: `medre-adapter-simplexchat`; SDK package
`simplex-chat` (its native library is prefetched by the adapter contract
job in CI).

| Leg                                      | Highest tier reached today | Basis                                                                                                                                                       | Automation                                        |
| ---------------------------------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| Exact-target native acceptance (G01/G02) | `sdk_contract`             | Provider-owned `tests/test_sdk_contract.py` at the pinned provider commit; outbound acceptance at this pin confirms only the native local queue.              | SDK contract tier runs in CI; a two-profile local tier on one host is not built yet and would be the next reachable step |
| SimpleX replies (G05, and register G08)  | `live`                     | Provider-owned `tests/test_native_peer_evidence_gate.py` at the pinned provider commit; the native-quote candidate path stays capability `fallback` pending an independently observed quote in a destination client. | Operator-run (independent peer profile)           |

Whether two SimpleX profiles on one host can produce an independently
observed quote cannot be verified from the pinned provider repositories
alone; the provider's own qualification gate is written against an
independent peer, so no local-tier reply qualification is claimed.

## Landed legs and outstanding legs

Landed on this campaign so far:

- Matrix exact-target native acceptance at the `docker` tier
  (`tests/integration/test_synapse_exact_target_acceptance.py`),
  CI-automatable through the Docker integration workflow. This is the
  first landed leg; it records the register evidence for G02's
  per-transport acceptance proof. The leg uses plaintext rooms and does
  not advance encrypted Matrix verification under G05.
- LXMF exact-target propagation with an independently observed recipient
  at the `local_integration` tier
  (`tests/integration/test_lxmf_exact_target_propagation.py`), collected
  and executed by the required `core-owned-lxmf-integration` job at the
  pinned adapter SDKs. This records the local rung of the G05 LXMF
  propagation claim over a loopback-only topology; external-network and
  hardware rungs remain open.

The core-authorized LXMF replay has also executed at its manifest pin through
its opt-in workflow. The required propagation job remains a different scenario.
MeshCore core replay has local real-SDK companion conformance coverage; this does
not close its physical-radio or recipient claims. Both core-owned replay suites
emit [revision-bound qualification records](native-qualification.md), preserving
actual SDK versions and exact attempt identity without uploading message bodies.

Outstanding legs, grouped by what each needs:

- CI-automatable now (harness exists on an already-run tier): the
  exact-target acceptance leg over containerized `meshtasticd` and the
  encrypted Matrix outbound leg on the existing Synapse E2EE environment.
- Needs a new local service tier: a containerized XMPP server harness,
  a containerized Chatmail relay harness, and a local two-profile
  SimpleX tier, each only if the pinned SDK can honestly produce an
  acceptance observation at that tier — to be established before any
  tier is labeled.
- Operator-run only (hardware or external peer): radio interruption
  (Meshtastic, MeshCore BLE/serial), native MeshCore physical-companion
  acceptance beyond protocol conformance, external LXMF propagation peering
  and physical RNode lifecycle, encrypted Matrix continuity under a live
  account, XMPP and Chatmail recipient verification, and the SimpleX
  reply observation by an independent peer.

Historical campaigns and their reported receipts remain recorded in
`docs/dev/native-evidence-index.json` as a frozen baseline; they do not
qualify the current pins, and this page's rows are the only current-pin
evidence map.
