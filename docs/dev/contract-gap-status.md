# Contract gap status

This page is the living register of open contract gaps between MEDRE's
current behavior and the delivery, content, and qualification contracts the
project has specified or reconciled against source. It records what is
missing, what is partially in place, and what is deliberately deferred — as
a status inventory, not a schedule. Each row carries its own source anchors;
those anchors, not this prose, are authoritative.

The register is seeded from a whole-stack source reconciliation (core plus
the seven independently distributed adapters) taken 2026-11-02 against
`main` at the retention lifecycle merge. Rows are updated in the same change
as work that moves them.

## Reading a row

Statuses form a small closed set:

| Status | Meaning |
| --- | --- |
| `open` | No behavior for the gap has landed on `main`. |
| `partial` | Some behavior has landed, but the contract is not fully met and no open change request currently advances it. |
| `in flight` | An open change request advances the gap, including when some behavior has already landed. This status takes precedence over `partial` while that request remains open. |
| `deferred` | Deliberately sequenced behind other work; not abandoned. |
| `out of scope` | Explicitly not a project goal at the current maturity. |

A row is `in flight` while a linked, open change request advances it, even
when existing `main` behavior is partial. After the change merges, reassess
against `main`: move to `partial` if the contract remains incomplete, or close
the row only after the qualification rule below. A local branch without an
open change request advances nothing. A gap closes only when its behavior is
implemented, specified under `docs/spec/`, and qualified on `main` — historical evidence
never closes a row. Native evidence tiers (fake, SDK, local service,
independent recipient, hardware) are recorded per claim and a lower tier
never stands in for a higher one.

## Register

| ID | Priority | Gap | Status | Current state and evidence | Next work |
| --- | --- | --- | --- | --- | --- |
| G01 | P0 | Exact-destination replay selection and decision | partial | `main` carries the typed preview, the shared execution-admission selector, the audited decision authority, and the operator command surface for decisions, cancellation, and exact-target replay scoping; fake-tier adversarial qualification covers real SQLite transaction races, restart durability, cancellation fencing, and sibling boundaries on core seams; the LXMF propagation scenario supplies a core-owned local-SDK evidence asset with separate recipient observation and discovery-refusal checks. Required `core-owned-lxmf-integration` CI executes it at the pinned SDKs and rejects missing or skipped runs. The scenario invokes `LxmfSession.send_text` directly, not the core replay selector or decision authority, so it does not close LXMF exact-target replay acceptance (`docs/spec/exact-target-recovery.md`). The typed native-evidence taxonomy and proof contract landed in `src/medre/core/evidence/native_evidence.py` (specified in `docs/spec/native-evidence.md`), projecting provider facts onto core-authoritative attempt identity without closing any leg. | Core-authorized native acceptance for LXMF and the other unqualified transports (including Chatmail and XMPP service tiers); external-network and hardware rungs. |
| G02 | P0 | Uncertain-native-handoff remediation | partial | Exact cancellation fences unstarted work and the typed preview classifies uncertainty without resolving it; the landed decision authority (#429) records auditable intent over unstarted work only and cannot decide an uncertain native handoff. The native evidence campaign (see `docs/dev/native-evidence-campaign.md`) now maps every per-transport acceptance leg to its strongest reachable tier, and the first leg has landed: on the Docker Synapse tier, an authority-authorized exact-target replay send is observed accepted by the real homeserver through a second Matrix client — independently of MEDRE's own receipts — while a selection without a persisted generation is refused before anything is staged. The typed native-evidence contract (`docs/spec/native-evidence.md`) now projects post-handoff and deferred observations onto core attempt identity under a bounded accepted / definitively_not_submitted / unresolved / not_observed vocabulary; it records observations only and closes nothing. | Remaining per-transport native-acceptance proof under the exact-target authority (Meshtastic over `meshtasticd`, MeshCore and LXMF local-integration endpoints, then the local service tiers and operator tiers the campaign page maps); never infer non-acceptance. |
| G03 | P1 | Delivery deadline and hard content privacy ceiling | partial | Ceiling producers, admission consistency, the native-submit deadline fence, the final synchronous dispatch-start fence and the strictly fenced cleanup transition landed: the `content_ceiling` policy drives admission, which installs and validates the ceiling in both content rows, the outbox adoption fence enforces the earlier-of effective deadline, `authorize_native_submission` refuses submissions past that deadline with no receipt or invented evidence, the pipeline observes that effective bound (`observe_effective_dispatch_bound`) from the same storage-authoritative rows and carries it to the adapter call site, where a final synchronous check with no intervening await refuses a new native submission past it, the radio packet-start grant rechecks the bound at every packet while preserving accepted prefixes, and `retire_expired_unstarted_outbox` retires only provably unstarted, unclaimed `pending`/`retry_wait` work past it under `BEGIN IMMEDIATE` compare-and-set. The retry worker's poll cycle now carries the sweep behind a default-off `retry.deadline_sweep` policy: enabled, each cycle runs at most one bounded sweep step and a sweep failure is isolated from the delivery poll; disabled, poll behavior is unchanged. The content-availability preview is not that enforcement contract; the enforcement design is documented in the delivery-deadline page under developer documentation. | Measure sweep writer latency on large mostly-ineligible queues before deciding enablement, and qualify the native-submit refusal before native SDK and hardware invocation, without weakening accepted or uncertain attempt authority. |
| G04 | P1 | Authorized plugin execution host and stage decisions | open | `src/medre/plugins` is protocol scaffolding; the exploratory draft branch is not a reconciliation candidate and any future runtime starts from current `main` primitives under the content-admission, grant-read, and outbox-settlement contracts. | A future runtime contract, if the capability is taken up: stage decisions, grant scope, and derived-event admission designed against current `main` before any execution host opens. |
| G05 | P1 | Recipient and native verification on the current pin set | open | Historical 42-direction campaign and local SDK tiers exist; the native evidence campaign (see `docs/dev/native-evidence-campaign.md`) now maps each per-claim leg to its strongest reachable tier on the current pins, anchored to the manifest pins and existing harnesses. The Synapse second-client observation basis for the provider side exists at the Docker tier, and the LXMF propagation leg now carries executed current-pin evidence: the required core-owned `core-owned-lxmf-integration` CI job runs the loopback propagation scenario with independent recipient observation at the pinned SDKs. Scoped current-pin native evidence remains outstanding for encrypted Matrix, radio interruption, SimpleX replies, XMPP, Chatmail. The typed native-evidence taxonomy (`docs/spec/native-evidence.md`) landed, mapping the Synapse second-client and LXMF loopback recipient observations onto the shared scope/tier/result vocabulary; per-claim current-pin qualification is unchanged. | Property-specific current-pin qualification on the tiers the campaign page maps; reuse historical proof as reference only. |
| G06 | P1 | Provider-owned copy and attachment retention authority | partial | Automatic collection covers the classified isolated core subset only; provider-side copies and attachments are outside it, and no physical-erasure claim is made. | Per-provider qualification after the hard deadline contract (G03). |
| G07 | P2 | Recent-context and time-bounded history profiles | open | Only the explicit relay-only automatic policy exists. | Design after G03 and G06; profiles cannot relax the hard ceiling. |
| G08 | P2 | SimpleX reply recipient-side qualification | partial | Native reply exists as a capability fallback; promotion waits on an independently observed quote in a destination client. | Provider vertical with immutable target ledger and independent peer proof. |
| G09 | P2 | Matrix peer-device trust and principal-aware sends | partial | Own-device signing exists; peer trust policy and principal selection are incomplete. | Separate transport/security vertical; crypto and native IDs require principal scoping. |
| G10 | P2 | Event enrich, transform, and policy stage conformance | deferred | Six pipeline phases are implemented and executed; plugin policy stages are not executed on `main`. | Lands with a plugin runtime if the capability is taken up (G04). |
| G11 | P2 | Guided operator recovery and first-run flow | partial | The exact-action operator commands — decision, cancellation, and exact-target replay scoping — are landed on `main` with their guides, and the typed observation model supports them; a simplified first-run journey remains absent. | Design the simplified first-run journey separately. |
| G12 | P2 | Adaptive transport-level rate limiting | partial | Global capacity bounds and adapter-local Meshtastic bounds exist; a general adaptive rate policy does not. | Operational robustness work; retry ownership stays unified. |
| G13 | P2 | Current seven-provider limitations provenance | partial | The limitations indexes are verified against all seven pins recorded in the adapter compatibility manifest: `known-limitations.md` records the inspected commits and each provider's native SDK pins, and `transport-limitations.md` covers all seven transports with per-claim pinned evidence. Claims that rest on observation rather than source (MeshCore BLE hardware, the LXMF loopback gate, Chatmail peer evidence) carry recorded evidence ages that a source pass cannot refresh. The LXMF loopback gate is additionally exercised at its pin by the required core-owned `core-owned-lxmf-integration` CI job, so that claim no longer rests on a dated observation alone. | Re-verify the indexes whenever the compatibility manifest pins move; replace dated observation evidence through the live/hardware tiers when those tiers run. |
| G14 | P3 | MMRelay canonical Matrix-shape collaboration | deferred | Current interop is a wire-compatibility boundary; broader shared event adoption is not committed. | Revisit after runtime contract stabilization; keep external compatibility isolated. |
| G15 | P3 | Distributed worker and multi-operator architecture | out of scope | Single-operator, single-machine operation is the supported deployment; retention collection is process-local. | Document the limit; no near-term action. |
| G16 | P3 | Dynamic route and configuration reload | out of scope | Startup-compiled routes are fixed for the runtime instance. | A later contract only if a concrete need appears; would require atomic authority rather than in-place edits. |

## Exact-destination recovery detail

The G01/G02 work stream resolves into five durable stages; the register row
tracks the whole, this section tracks each stage:

1. **Typed selection contract and preview** — frozen selection key,
   observation vocabulary, metadata-only preview. Landed in #424.
2. **Shared selector** — one identity selector used by both preview and
   execution admission, rejecting route, recipient, and plan drift before a
   send is staged. Landed in #427.
3. **Decision authority** — audited compare-and-swap over an observed exact
   target, reusing the attempt and claim fences; no implicit resend of
   queued, in-progress, or uncertain native work. Landed in #429.
4. **Operator command surface** — explicit intent, confirmation, and
   refusal rendering wired to the contract; `medre recover` stays read-only.
   Landed in #430.
5. **Adversarial qualification** — real-transaction races, restart,
   cancellation, and current-pin native evidence for the exact-target
   property. Fake/core-seam qualification — real SQLite transaction races,
   restart durability, cancellation fencing, and sibling boundaries — is
   in place, and the LXMF native local-SDK rung is executed by the
   required core-owned `core-owned-lxmf-integration` CI job at the pinned
   SDKs, with the job failing on not-collected or skipped runs. Remaining:
   the Chatmail and XMPP native legs (each needs a CI-executable service
   tier), and the external-network and hardware rungs.

Hard boundaries for every stage: native handoff is not recipient receipt; a
stale observation never permits a send; collected content is never
reconstructed; one target never widens into sibling channels.

## Maintenance

- A change that lands, advances, or closes gap work updates the affected
  row (and the detail section when relevant) in the same change, like the
  schema and fragment couplings in `change-process.md`.
- Rows link their in-flight change requests inline; when such a change
  merges, the row's status and evidence are rewritten in the merging change
  rather than left describing an open request.
- The register carries no dates beyond provenance and evidence ages; it is
  not a schedule, and row order encodes priority, not sequence.
