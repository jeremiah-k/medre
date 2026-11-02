# Decide one exact delivery target locally

This command records an audited operator decision over one observed delivery
target: `resume_scheduled` is the audited go-ahead for unstarted scheduled
work to resume through the normal retry fences, and `resolve_uncertain`
closes the operator's open review of that target without intervention. Both
actions are audit-only acknowledgements. The command never sends a message,
never stages a delivery, and never changes the observed outbox row.

The state file requirements are the same as for
[exact cancellation](exact-cancellation.md): a private POSIX SQLite file with
mode `0600` inside an owner-only directory (mode `0700`), no symlink
components, no parent traversal, no hard links, ancestors controlled by the
file owner or root, and sticky-bit protection for writable ancestors. The
command is supported only on POSIX. An actor label is unverified provenance,
never a credential.

## Preview the observed target

Given the full identity — event, delivery plan, adapter, and an explicit
channel decision (`--target-channel CHANNEL` or `--no-channel`, never an
empty string) — the preview reports the current persisted state without
authorizing anything:

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --action resume_scheduled
```

The JSON response is metadata only. It always carries
`apply_authorized: false` and the full observation (`category`, raw
`attempt_number` and `active_attempt` counters, `cancel_eligible`,
`observed_fingerprint`). When the observed state is decidable — the row is
`pending` or `retry_wait` with no reservation and no claim — the response
also carries an `observed` block with the exact `outbox_id`,
`expected_generation`, `expected_status` and `expected_updated_at` an apply
must copy. When the observed state is not decidable, the response carries
`refusal_implications` naming the frozen refusal(s) the authority would
raise, and no apply shape: an observation never becomes a mutation permit.

| Observed category                              | Meaning for a decision                                        |
| ---------------------------------------------- | ------------------------------------------------------------- |
| `pending`, `retry_scheduled`                   | Decidable when unreserved and unclaimed.                      |
| `in_progress`, `awaiting_feedback`             | Never decided from an observation.                            |
| `inspect_native`                               | Possible native acceptance; resolve that uncertainty first.   |
| `preview_replay`, `needs_operator_action`      | Terminal failure; reopening needs a send-capable authority.   |
| `completed`, `cancelled`                       | Settled outcome; not decidable.                               |
| `not_found`, `ambiguous`                       | No single observable target; the authority refuses.           |
| `content_unavailable`, `route_or_plan_changed` | Re-observe the target before deciding.                        |

## Apply the observed decision

Review the event's current evidence first. Apply only from a fresh preview,
copying _all_ observed values exactly:

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --action resume_scheduled --apply --yes \
  --decision-id UNIQUE_DECISION --actor-label LOCAL_OPERATOR \
  --outbox-id OBSERVED_OUTBOX --expected-generation OBSERVED_GENERATION \
  --expected-status OBSERVED_STATUS --expected-updated-at OBSERVED_VERSION
```

A first application reports `{"decision_id": ..., "committed": true}`;
repeating the same exact decision reports `committed: false`. Any drift — a
reservation, a claim, a newer generation, a moved update version, or a reused
decision ID bound to different parameters — is refused with the frozen
refusal string on stderr and nothing is recorded. The choice between
`resume_scheduled` and `resolve_uncertain` is recorded intent only: neither
action dispatches work, and a later claim that follows a committed decision
is the decided outcome, not a conflict.

## Inspect the recorded decision

Read back the durable audit record with `medre inspect decisions --event
EVENT --storage-path /private/state.sqlite` (see
[Operator decisions](operator-decisions.md)). Audit rows carry the action,
the observed state the decision CAS-guarded, the actor label and the
recording time.

## Exits and scope limits

Success exits `0`. Malformed flags or an incomplete identity exit `2`; a
refused decision exits `1` with the frozen refusal string; storage and
local-permission failures exit `3`.

Limits: the command decides only unstarted scheduled work under one exact
identity. It cannot cancel (use
[exact cancellation](exact-cancellation.md)), cannot execute a replay (see
[recovery and replay](recovery-and-replay.md) for the `--exact-target`
replay scoping flag), cannot reopen terminal failures, and never widens to
sibling destinations.
