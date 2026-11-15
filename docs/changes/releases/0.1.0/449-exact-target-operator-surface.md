# Exact-target operator surface

`medre resolve` gains an `--action` decision mode beside the unstarted
cancellation, under the same private POSIX owner-only state-file boundary.
The preview calls the typed exact-target observation and renders metadata
only — the observed category, raw generation counters, `cancel_eligible`
and, when the observed state is not decidable, the frozen authority
refusal(s) the state family would raise. Only unstarted scheduled work
renders as decidable, together with the exact `outbox_id`, generation,
status and update version an apply must copy; every other category suggests
no apply shape, so an observation never becomes a mutation permit.

Apply requires `--yes`, an actor label, a decision ID, one of the frozen
`resume_scheduled`/`resolve_uncertain` actions and the observed state, and
reports the decision ID with its idempotent commit flag. Refusals exit with
the frozen refusal string; storage and permission failures keep their
existing exit codes. The command records audit only: it never stages a
send, never mutates the observed row, and resumption stays with the normal
claim/attempt fences.

`medre replay` accepts repeatable `--exact-target EVENT:PLAN:ADAPTER[:CHANNEL]`
and `--exact-no-channel EVENT:PLAN:ADAPTER` flags that name complete
delivery identities for `ReplayRequest.exact_target_selections`. Three
components name the deliberate no-channel identity; the channel is
everything after the third colon, so colon-bearing channels stay intact. An
empty channel component, an incomplete identity, a channel-bearing
specification passed to `--exact-no-channel`, and one identity repeated
across specifications are configuration errors rejected before any
configuration is loaded. Valid selections only restrict delivery admission;
the dry_run default guidance and every side-effect-free flow are unchanged.

The recovery runbook keeps its adapter/route warning for replays without an
exact selection and, for channel-scoped previewable failures, also names
the exact-target replay flag. The operator guide for the decision flow is
`docs/ops/exact-target-decisions.md`.

Replay request validation runs before configuration loading or adapter startup.
Smoke-backed replay CLI qualifications require registered fake providers and
belong to the adapter compatibility tier; the core suite retains request
validation, exact-target parsing, and audited decision tests. Exact-target
flags restrict only side-effecting delivery admission: `dry_run` reports
route/plan/render stages without testing that admission scope.

Decision previews open their private state file writable because a content
read may record a durable mandatory dependency expiry observation; they do
not change the observed outbox row or authorize delivery.

Replay selections reject whitespace-only event, plan, adapter, or channel
components before loading configuration. Operator guidance distinguishes the
boolean no-channel inspection flag from the replay flag, which requires the
full `EVENT:PLAN:ADAPTER` identity argument.
