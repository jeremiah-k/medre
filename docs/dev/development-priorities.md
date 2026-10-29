# Development priorities

MEDRE's delivery foundation has durable fanout, scoped native references,
generation-bound ownership, restart recovery and conservative confirmation
semantics. Build visible messenger features and usable recovery on that
foundation. Keep protocol details in providers and delivery authority in core.
A capability declaration is an implemented and qualified contract, rather than a
list of upstream protocol possibilities.

## Coordinated work program

| Order | Work package                                    | User-visible outcome                                                                                             | Acceptance boundary                                                                                                                                                                 |
| ----- | ----------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1     | Current recovery guidance                       | Explain whether each destination is waiting, active, uncertain or finished, and offer a scoped preview.          | Shared authority for recover/evidence, admitted-before-receipt work, no native resend, schema and SQLite/CLI regressions.                                                           |
| 2     | Content lifecycle and expiry                    | Choose relay-only, recent-context or history retention without losing retry ownership.                           | Explicit persisted content identity/availability, delivery and reply holds, bounded optional/plugin grants, hard privacy ceiling, metadata-only preview before collection.          |
| 3     | Exact-destination recovery preview              | Recover one failed conversation without including completed sibling channels.                                    | Typed destination selection used identically by preview and delivery; fail closed on ambiguous or changed plans; generation and named-run identity retained.                        |
| 4     | SimpleX native reply vertical                   | Display a real quote in a destination client while retaining sender/origin text.                                 | Immutable profile/user/conversation/item target in ledger, pinned native lookup/send contracts, fallback and budget checks, restart fence, independent peer proof before promotion. |
| 5     | Guided first-run and presentation               | Connect two conversations without reading deployment or protocol internals first.                                | Short install/check/connect/route/run journey, linked deployment reference, honest storage notice, runnable fake presentation example and explicit supported/extension boundaries.  |
| 6     | Recovery decisions for uncertain handoffs       | Record a deliberate operator resolution with an inspectable audit trail.                                         | Proof-specific decision model, compare-and-swap against exact observed generation, stale decision refusal, no inferred non-acceptance and no implicit resend.                       |
| 7     | Principal-aware Matrix application services     | Distinguish bot display attribution from authorized virtual-user or account sends.                               | Persist the selected principal before native dispatch, scope references/crypto to it, prove restart and mutation authorization with real peers.                                     |
| 8     | Bounded operational readiness and qualification | Understand encrypted-room behavior, context fidelity, sustained fanout, backlogs, shutdown and storage pressure. | Feature-specific independent peers, mixed-workload measurements, bounded queues and secret-free diagnostics; retain the existing all-pairs baseline.                                |

Current recovery guidance is implemented. Exact-destination inspection and
per-event content dependency counts establish read-only boundaries; neither
executes recovery nor authorizes expiry. An exact-cancellation storage authority and a versioned content policy preview
provide the next building blocks. Their operator workflow, persisted content
identity and runtime enforcement remain the next core implementation work.

Chatmail native replies have passing dedicated two-profile GreenMail
qualification, including native quote and sender/origin presentation after
reconnect and restart. SimpleX now has a scoped quote candidate with durable
ownership and native-parent validation; its advertised reply capability remains
`fallback` until independent recipient-side qualification. Keep that distinction
in operator docs and machine-readable profiles. Privacy policy design and
optional plugin retention share the
[content lifecycle proposal](content-lifecycle.md); presentation alternatives
and application-service scope are described in the
[presentation guide](../ops/message-presentation.md).

## Prerelease development contract

Develop against one current schema and API contract. Reject incompatible
prerelease state and use fresh state. Provide no migrations or legacy readers.
Provide no version aliases or compatibility shims. Version fields identify the
supported format and allow strict refusal; they do not promise an upgrade path
from earlier development formats.

## Architecture decisions

- Reuse `DeliveryAuthorityResolver` for present delivery state. Receipt append
  order remains historical evidence where a mutable generation owns the target.
- Keep guidance read-only. Execution requires a separate decision interface;
  avoid adding write flags to a diagnostic command or bypassing attempt fences.
- Model exact replay selection with typed destination identity rather than a
  string combining adapter, channel and plan. Preserve context kinds and native
  IDs; do not interpret transport addressing in core.
- Reuse core-computed relation target facts for native conversation features.
  Providers validate their own native profile and conversation immediately before
  submission. Do not reconstruct trusted bindings from rendered text or metadata.
- Bind reply targets and fallback bytes to durable attempts. An accepted operation
  stays authoritative across renderer changes; uncertainty never becomes proof
  of non-acceptance merely because a worker or process restarted.
- Keep source author, display attribution and authenticated bridge account
  separate. Preserve readable attribution when a native feature falls back.
- Add shared interfaces after two real implementations establish a common need.
  Do not create a universal messenger or queue framework around hypothetical
  attachments, read markers, puppeting or upstream SDK features.
- Qualify properties with focused recipient observations. Preserve the existing
  42-direction baseline instead of repeating it for every provider change.

## Execution and review

Each work package needs an independently reviewable branch and atomic commits by
concern. Runtime changes include tests, specification, schema where applicable,
and a change fragment. Provider features qualify against an exact core revision;
core selects exact landed provider revisions after their own CI passes.

Use targeted domain tests and pinned SDK contracts locally. Let CI run full
provider/core suites and supported-Python matrices. Record real peer observations
separately from mock/SDK-shape contracts. Merge only the work that has met its
own acceptance boundary; unrelated qualification campaigns do not block progress
on independent packages.
