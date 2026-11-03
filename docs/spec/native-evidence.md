# Native evidence observation contract

## Scope and authority

The typed native-evidence contract in
`src/medre/core/evidence/native_evidence.py` projects native-evidence
observations into one scoped, immutable envelope:
`NativeEvidenceObservation`. The package `__init__` re-exports the
vocabulary; consumers may import from either path. The contract is
additive: it wraps the existing
`PostHandoffObservation`, `DeferredHandoffCompleted`, and
`DeferredHandoffFailed` delivery-feedback facts without replacing or
competing with their pipeline semantics, changes no existing output,
schema, or behavior, and exposes no CLI surface and no storage writes.

The envelope is purely typed and in-process. It defines no serialized
JSON projection, so no JSON Schema is published for it in `docs/schemas/`.
If a serialized projection is introduced later, its schema lands in the
same change that introduces the projection.

Construction fails closed. Malformed input — an incomplete delivery
identity, an empty-string channel, an unknown tier, scope, or result, a
naive observation timestamp, an empty or whitespace-bearing identifier
where a concrete value is required, or a result/scope/tier pairing the
taxonomy forbids — raises before an envelope exists.

## Envelope fields

| Field group | Fields | Meaning |
| --- | --- | --- |
| Attempt identity | `event_id`, `delivery_plan_id`, `target_adapter`, `target_channel` | The complete event-scoped delivery identity, carried as a `DeliveryIdentity` value object. `None` is the deliberate no-channel identity; the empty string is rejected. |
| Attempt anchor | `outbox_id`, `attempt_number`, `observed_row_updated_at` | The exact outbox row the evidence is about: its identifier, the raw persisted attempt counter (never display-normalized), and the observed row update version at observation time. |
| Provider binding | `provider`, `provider_repository`, `compatibility_pin` | Which provider the claim is about and the compatibility-manifest pin in force when the observation was made. |
| Observation context | `observation_source`, `tier`, `scope`, `observed_at` | What produced the fact, the evidence tier, the claim family, and the timezone-aware observation timestamp. |
| Result | `result` | One bounded result token (below). |
| Identifiers | `receipt_id`, `native_message_id`, `evidence_reference` | Optional identifiers of an existing core receipt, a provider-native message, or a persisted evidence record. Identifiers only. |
| Derived | `identity`, `evidence_fingerprint` | The normalized `DeliveryIdentity` and a durable fingerprint over the canonical fact statement. |

The fingerprint is a SHA-256 digest over the canonical fact statement —
every field above except `observed_at`. The observation timestamp is
excluded deliberately: the fingerprint identifies the observation
content, so the same provider fact recorded twice, including across a
persistence or restart boundary, yields the same fingerprint and
duplicates stay detectable. Equal facts always produce equal
fingerprints. A fingerprint identifies the supplied statement; it does not
attest that the observation occurred, that a tier or pin is accurate, or that
the stated attempt is current in the ledger.

## Result taxonomy

The result is a closed four-token vocabulary. No result by itself
establishes ledger binding or authorizes recovery:

| Result | Meaning |
| --- | --- |
| `accepted` | The observation establishes the provider-specific claim that the send stood as a provider submission. |
| `definitively_not_submitted` | The observation establishes the send never became a standing provider submission. |
| `unresolved` | The observation establishes neither. It is a real result, not a gap to be filled by inference. |
| `not_observed` | No provider observation is available to the constructing caller for this attempt. It is an explicit absence-of-local-evidence statement with its own fingerprint and no identifiers, not proof of a provider-wide negative search. |

The central rule of this contract: **`unresolved` never becomes
permission to resend, and absence of evidence never proves rejection.**
An `unresolved` observation licenses exactly nothing. A `not_observed`
record MUST be treated as `unresolved` for remediation purposes — the
envelope's `resolution` projection folds it accordingly — while the raw
result keeps the distinction between "observed and inconclusive" and
"never observed". A `not_observed` record is never conflated with
`definitively_not_submitted`.

## Scope vocabulary

The scope names the claim family an observation belongs to, not its
polarity: an observation of a provider refusal is still
`provider_acceptance`-scoped evidence whose result is negative.

| Scope | Claim family |
| --- | --- |
| `native_handoff` | Facts about the transport hand-off boundary itself. |
| `provider_acceptance` | Facts about how the provider handled the submission. |
| `recipient_observation` | Facts established by an independent recipient side. |

Construction rejects result/scope and result/tier claims that exceed their
proof boundary:

- `accepted` at `native_handoff` scope — a hand-off-boundary fact alone
  never establishes acceptance, including under the strongest
  adapter-reported confirmation level;
- `definitively_not_submitted` at `recipient_observation` scope — a
  recipient-side fact cannot establish non-submission;
- `accepted` at `fake_pipeline` or `sdk_contract` tier, in either provider
  or recipient scope — these tiers cannot establish provider acceptance
  or independent recipient observation;
- `definitively_not_submitted` at `provider_acceptance` scope with either
  of those two tiers — synthetic or SDK-contract evidence cannot establish
  an actual provider refusal. A pre-handoff failure can still establish
  a `native_handoff`-scoped negative at the boundary it exercised.

## Tier proof table

Tiers reuse the campaign's established vocabulary and the transport
realism ladder (`appendices/transport-realism.md`). A lower tier never
stands in for a higher one.

| Tier | Can establish | Cannot establish |
| --- | --- | --- |
| `fake_pipeline` | Core-seam transactional properties over a synthetic adapter boundary. | Any provider-side fact; a fake-tier handoff is never read as provider acceptance. |
| `sdk_contract` | Behavior of the exact pinned SDK surface. | Provider-side acceptance of a send. |
| `local_integration` | SDK and session interaction against a deterministic local endpoint or in-process peer: local router or session acceptance, and recipient-side observation over a loopback topology. | External network arrival, propagation peering, or any behavior beyond the exercised local boundary. |
| `docker` | Provider acceptance observed against a real containerized provider service, including through an independent second client of that service. | Federation, external peers, or behavior beyond the containerized service class. |
| `live` | Facts against an external service or an independent peer. | Nothing beyond what the external party actually exposes; operator-run by design. |
| `hardware` | Facts against physical devices. | Nothing beyond the exercised hardware path; operator-run and opt-in. |

## Authority rules

1. **The core delivery ledger is authoritative for attempt identity.**
   For the three wrapped-fact projections, identity, outbox, and generation
   come from the core-issued `DeliveryAttemptProvenance`, and those
   projection constructors accept no identity override. Direct
   `NativeEvidenceObservation` construction and `native_evidence_not_observed`
   take identity fields from the caller, because no wrapped provider fact
   supplies them. These caller-supplied fields require independent ledger
   verification before they are treated as authoritative attempt evidence.
   Projection constructors preserve typed provenance but do not authenticate
   its issuer or query the ledger either; their callers must use the existing
   feedback/provenance validation before treating that binding as authoritative.
   Provider identity, compatibility pin, source, and tier are caller assertions
   whose actual observation basis must also be verified independently.
2. **A provider fact is authoritative only for the provider-specific
   claim it can actually establish.** The projections below are the
   complete mapping; no other claim may be derived from a wrapped fact.
3. **Independent recipient corroboration does not change a core
   generation.** Corroborating evidence binds to the same generation the
   core ledger states; it never advances, rewrites, or re-numbers an
   attempt.

| Wrapped fact | Scope | Result |
| --- | --- | --- |
| `PostHandoffObservation` state `delivered` | `provider_acceptance` | `accepted` |
| `PostHandoffObservation` state `rejected` | `provider_acceptance` | `unresolved` |
| `PostHandoffObservation` state `failed` or `cancelled` | `provider_acceptance` | `unresolved` |
| `DeferredHandoffCompleted` (any confirmation level) | `native_handoff` | `unresolved` |
| `DeferredHandoffFailed` (any outcome) | `native_handoff` | `definitively_not_submitted` |
| No provider observation | caller-selected | `not_observed` |

`rejected`, `failed` and `cancelled` post-handoff states occur after
successful transport hand-off. They cannot establish that the provider
never accepted the submission, so they project to `unresolved` rather
than to a definitive negative claim. A `DeferredHandoffFailed` fact's
own contract establishes that the attempt terminated before transport
hand-off, so every outcome
projects to `definitively_not_submitted`.

The `resolution` projection is a classification, not an authority grant.
The tier, scope, attempt binding and actual observation basis remain
necessary for any later reconciliation decision; reading `resolution`
alone cannot establish proof or authorize any state change.

## Identifier discipline

Projections omit the wrapped fact's error text and adapter metadata. The
selected receipt/native-reference identifiers are copied verbatim: the
envelope does not inspect or redact their contents, nor does it verify that
an identifier refers to an existing ledger or provider record. Callers MUST
supply identifiers without message content or credentials and verify their
binding before consumption. The `not_observed` result forbids all three
identifier fields, because none were observed.

## No execution authorization

The envelope is a record, never a permit. Nothing in this contract
authorizes a send, a resend, or a lifecycle transition, and the
vocabulary deliberately contains no execution-authorization concept.
Staleness (`is_stale_for_generation`) is an observation for remediation
reasoning; a stale observation never permits a send, and a current one
does not either.

## Transport mapping on the current pins

The mapping below records only what the current harnesses actually
establish. No common SDK behavior is assumed and no universal
provider-status interface is defined; the shared interface waits until
two working native implementations exist.

- **Matrix.** The Synapse second-client timeline observation
  (`tests/integration/test_synapse_exact_target_acceptance.py`) is
  `docker`-tier, `provider_acceptance`-scoped, `accepted` evidence for
  the replay identity it observed through a second client of the real
  homeserver.
- **LXMF.** The loopback recipient delivery callback
  (`tests/integration/test_lxmf_exact_target_propagation.py`) is
  `local_integration`-tier, `recipient_observation`-scoped, `accepted`
  evidence for the locally observed propagation, terminated at the same
  loopback instance that runs the propagation node. The provider-owned
  local router acceptance harness
  (`tests/test_lxmf_local_integration.py`) is `local_integration`-tier,
  `provider_acceptance`-scoped evidence of local router acceptance only.
- **LXMF provider-level scenario.** The loopback scenario invokes the
  provider send surface directly rather than the core replay selector or
  decision authority, so it is provider-level evidence and NOT
  core-authorized: it does not project a core-authorized exact-target
  acceptance claim, and LXMF core-authorized native acceptance remains
  open work recorded in the campaign page and the gap register.
