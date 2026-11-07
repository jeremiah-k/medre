# State-machine ownership audit — 2026-11-06

Source: `medre-stack_2026-11-06_08-46-47.tar.gz`, all eight bundled
`main` revisions. This is a **source-inventory and disposition**, not an
additional runtime supervisor or permission to change native-send authority.

## How to classify a state cluster

- **Authoritative state machine:** state determines whether work can occur or
  whether an irreversible record may be written. Transitions need an owner,
  legal edges, and tests against late callbacks and cancellation.
- **Orthogonal state axes:** independent facts (connected, reconnecting, epoch,
  native task, cursor) must not be collapsed into one enum if combinations are
  real and observable. Each axis needs an ownership/fencing invariant.
- **Derived projection:** diagnostic counts, health labels, or a summary of
  persisted facts are not independently mutable state machines.
- **Resource/transaction state:** locks, leases, pending futures, and durable
  reservations may encode state without a public status enum. Their cleanup
  and handoff rules are the authority; do not invent retries or statuses.

## MEDRE core

| Source | State/transition owner | Disposition |
| --- | --- | --- |
| `core/engine/pipeline/delivery_state.py`, SQLite outbox methods | Durable outbox and receipt vocabularies, claim/reclaim/terminalization | **Already explicit.** Preserve status tables, atomic guards, attempt identity and reconciliation. No generic engine. |
| `core/lifecycle/states.py`, `runtime/app.py` adapter-state map | Eight adapter lifecycle states and `VALID_TRANSITIONS` | **Already explicit.** Runtime owns transitions; provider owns native health. |
| `runtime/app.py` `RuntimeState` | `INITIALIZED`, `STARTING`, `RUNNING`, `STOPPING`, `STOPPED`, `FAILED` | **Fixed in this slice:** validated transition graph before state mutation or event emission; stopped is terminal. |
| `core/engine/phases.py`, pipeline runner | Six execution phases, phase counters | **Already explicit** phase vocabulary; counters are observability, not separate authorization. |
| `core/engine/replay/types.py` | Replay execution status and side-effect eligibility | **Explicit replay model**; no new send authority. |
| `runtime/retry.py` `RetryWorkerState` | Running, shutdown, retained/abandoned tasks, claims | **Typed, independent process-local axes.** Preserve bounded stop/restart fence; durable outbox remains authoritative. |
| `runtime/collection.py` `ContentCollectionWorkerState` | Policy activation, shutdown, cursor epoch | **Typed, process-local axes.** Retention authority remains scoped to existing storage. |
| `core/ingress/worker.py`, ingress SQLite lease | Lease ownership, active event, cancellation | **Resource + durable lease machine.** Preserve guarded claim/reclaim; active event ID is not a separate delivery state. |
| `runtime/app.py` pressure buffer + outage accounting, SQLite gap table | Staging, open gap, persisted recovery, discarded counters | **Typed accounting and idempotent durable record.** Do not equate producer telemetry loss with durable admission or zero refusals. |
| `core/supervision/capacity.py` | Runtime admission permits and fairness waiters | **Capacity resource protocol.** Slots/waiters are not message lifecycle states and do not imply receipt or acceptance. |
| `runtime/route_engine.py`, route plans | Route eligibility and operational projections | **Explicit/derived.** Not another mutable routing authority. |
| `runtime/typing.py` | Transient composing/lease activity | **Ephemeral coalescing.** Never persist it as a receipt or use it to grant send permission. |

## Transport providers

All paths below are relative to the named adapter repository under `src/medre/adapters/` unless another root is shown.

| Provider/source | State axes | Disposition and invariant |
| --- | --- | --- |
| Matrix `matrix/session.py` | Client, durable sync owner, sync task, stop/reconnect flags, initial cursor, room and crypto observations | **Orthogonal.** Native SDK owns crypto/cursor; MEDRE owns admission. Durable owner and classic paths must not be flattened into a fake universal connection state. Keep ownership/cancellation tests and explicit checkpoints. |
| Matrix `matrix/adapter.py` | Startup/health epochs and native trust policy | **Epoch-fenced.** Retired health callbacks cannot mutate current-session observations; local trust is not cross-client verification. |
| Matrix `matrix/oauth_session.py` | Token freshness, record stamp, credential lock, refresh/rotation | **Transactional refresh protocol.** Lock and persisted credential identity outrank cached token; avoid a second refresh supervisor. |
| Meshtastic `meshtastic/session.py` | Started, stop requested, reconnecting, connection generation, liveness probes | **Orthogonal and generation-fenced.** SDK reader-thread callbacks must match their native generation. Stop cannot imply RF receipt. |
| Meshtastic `meshtastic/adapter.py`, `send_correlation.py`, ingress reservations | Callback staging capacity, accepted packet identity, ACK/NAK correlation, closed/retired generation | **Bounded resource and correlation protocols.** Keep pre-scheduling reservation and native-generation fences; ACK correlation cannot authorize delivery evidence. |
| MeshCore `meshcore/session.py` | Started, stop requested, initial connection task, reconnect task and native subscriptions | **Orthogonal recovery protocol.** Do not convert a recoverable disconnect into terminal failure; stop/cancel owns the connection. |
| MeshCore `meshcore/adapter.py` | Admission epoch, task set, pending callbacks, provisional dedup | **Generation-fenced reservation protocol.** Late completions cannot release current-session slots or suppress current-session retransmits. |
| LXMF `lxmf/native_process.py` | Worker spawn, readiness, stopping task, forced cleanup | **Resource ownership protocol.** Worker process, not the Python SDK in the parent, owns native lifetime. |
| LXMF `lxmf/worker_channel.py` | Pending request IDs, closing, failed, Stopped ACK, reader task | **Bounded RPC correlation.** A cancelled caller may retain late reply correlation; a crossed native submission cannot be relabelled retryable. Converting this to a single phase enum would discard essential independent facts. |
| LXMF `lxmf/worker_session.py`, `lxmf/session.py` | Worker generations, stop request, reconnect, feedback and delivery context | **Owner-scoped async work.** Preserve the immutable native handoff journal and its uncertainty fence. |
| Chatmail `chatmail/session.py` | One-shot use, closing, active off-thread RPC tasks, profile lease, native process | **Explicit resource protocol in code but implicit phase vocabulary.** The current spec intentionally releases the lease after failed drain; changing that policy requires an SDK-backed owner/lease decision. A future dedicated PR should test off-thread startup surviving timeout and process reaping. |
| Chatmail `chatmail/state.py` | Durable queued/submitting/accepted/uncertain handoff journal | **Persistent correlation machine.** Do not resubmit an ambiguous native handoff on restart. |
| SimpleX `simplexchat/session.py` | One-shot use, failed, closed, native reaped, owned process group, pending calls, profile lease | **Constrained multi-axis resource state.** Lease is deliberately held on uncertain cleanup; avoid collapsing `reaped-but-lease-unreleased` with `closed`. Existing ownership tests cover that distinction. |
| SimpleX `simplexchat/adapter.py` | Health epoch, native session state, staged admission | **Epoch-fenced observation.** Native `ready`/`result` records do not establish peer receipt. |
| XMPP `xmpp/session.py` | Connection runner, ingress/typing workers, accepting flag, health, stop | **Fixed in this slice:** single checked owner phase, bounded stop, retained unretired tasks, stale native health fence and safe retry of cleanup. |
| XMPP `xmpp/adapter.py` | Session ownership and health-report epoch | **Fixed in this slice:** failed stop does not silently drop the native session reference. |
| XMPP `xmpp/connection.py` | Slixmpp handshake, MUC joins, typing negotiation and shutdown | **SDK-owned/ephemeral axes.** Preserve independent correlation and capability negotiation; do not make local stanza writes delivery receipts. |

## Follow-up decisions, not implicit changes

1. **Chatmail thread/lease policy:** Current `docs/spec/chatmail.md` expressly
   requires releasing the profile lease even if off-thread drain times out.
   That is a documented liveness tradeoff, not an undocumented state we may
   silently reverse. Qualify the native process/thread relationship first;
   if a timed-out operation can create another native owner after release,
   amend the protocol and spec with an explicit fenced/retired phase.
2. **Matrix E2EE:** Peer-device local trust and own-device cross-signing are
   different ownership domains; do not invent a universal verification state.
3. **LXMF and SimpleX:** Do not turn an uncertain native handoff into a
   retryable refusal by resetting a boolean or by normalizing phase names.
4. **Transport reconnection:** Meshtastic and MeshCore have genuinely
   concurrent connection, callback, and pending-delivery axes. Keep their
   generation fences; a single enum would be misleading without a stronger
   mutually exclusive ownership model.
5. **Runtime health:** Native health and `RuntimeState` are intentionally
   distinct. One describes peer transport observation, the other process
   control. A health transition must not mutate durable work or vice versa.

## Qualification

The scan classified source classes with three or more state-like fields,
reviewed the named state/protocol owners in all eight checkouts, and compared
those clusters with existing specs/tests. It does not pretend Python's
possible runtime states or dynamically loaded SDK internals can be fully
enumerated by static search. Changed behavior is qualified separately in
focused transition and cancellation tests; native integration remains CI-/lab-
gated.
