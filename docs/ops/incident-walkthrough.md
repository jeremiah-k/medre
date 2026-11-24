# Incident walkthrough: a message did not arrive

A decision order for one delivery that a recipient says is missing. The
order is read-only until you have evidence for a fork, and every fork names
the command, how to read the output, what absence looks like, and the next
command. The action commands at the end each send, rule, or cancel exactly
one delivery target; each is previewed first, confirmed separately, and
irreversible once applied.

Local facts never become recipient facts. A `sent` receipt, a `queued`
receipt, a completed outbox row, and a clean recovery scan are records of
what this relay observed; none of them tells you whether the message
reached the recipient's client. When the order below ends without an
answer, that is the honest answer.

## Record the installation, then read-only

Write down `medre version` output first — every report below repeats that
string in its `medre_version` field, so a mismatch means the evidence came
from a different build:

```bash
medre version
```

All investigation commands below are read-only. `inspect`, `recover`,
`storage ceiling audit`, `storage ceiling verify`, `readiness`, and
`diagnostics` without `--refresh-health` start no adapter, send nothing,
and write nothing (diagnostics with `--refresh-health` does start the
runtime briefly; step 3 flags it).

The examples use `bridge.yaml` for the running configuration and
`/path/to/medre.sqlite` for the SQLite state file (`medre paths` prints the
default `Global DB`; use the `storage.path` your config selects if it
differs).

## 1. Is the delivery configured?

```bash
medre readiness --config bridge.yaml
```

Read: the `Configuration:` line (`valid` plus adapter and route counts),
and one `Adapters:` row per instance with `preflight passed` or a `blocked`
category such as `config_invalid` or `sdk_distribution_missing`.

Absence: `config_not_found` or `validation_error` — the runtime that
processed the message, if any, was running different settings than you
think. Fix the configuration before reading further; nothing downstream
can be interpreted against a config that was not in force.

Next: if configured, continue. If the configuration is valid but the
adapter row is missing, the adapter was never configured — the message had
no route.

## 2. Is the adapter installed?

```bash
medre readiness --config bridge.yaml
medre adapters
```

Read: the readiness manifest row for your transport (`installed` with
`registration registered` and a version), and the `Installed adapters:`
list with a version and source label.

Absence: `missing` (distribution not installed) or `unregistered` (installed
but its entry point is quarantined — readiness prints repair guidance);
the transport absent from `Installed adapters:`. An adapter that was not
installed when the message was sent could not have accepted it.

Next: if installed, continue. Note the installed version — an upgrade
between send and investigation changes what the runtime was.

## 3. Is the runtime running and connected?

`diagnostics` without `--refresh-health` is a build-time snapshot: every
adapter reads `health unknown` and `live_health` is null. That is the
offline answer, not an outage. The one command that observes the runtime
starts the configured adapters, polls each health check once, and stops
the runtime again — with real adapters it opens real connections:

```bash
medre diagnostics --refresh-health --config bridge.yaml
```

Read: every configured adapter id with `health healthy` and
`adapter_state ready` (JSON: `health.live_health.adapters.<id>`).

Absence: a startup failure exits non-zero (`3` build, `4` total startup
failure) or reports adapters `degraded`, `unavailable`, or `failed` — the
runtime was not able to accept or deliver while it was down. Nothing
reports `observed` from a stopped runtime; see
[troubleshooting](troubleshooting.md) for the startup drills before
continuing.

Next: if the runtime was up and connected during the incident window,
continue to the stored event.

## 4. Was the message stored at all?

If you know the source message's native id (the id the source client shows
for the message), resolve it to the canonical event:

```bash
medre inspect native-ref --adapter bot --channel '!room:example.com' \
  --message '$SOURCE_MESSAGE_ID' --storage-path /path/to/medre.sqlite
```

Read: a ref with `direction: "inbound"` and `resolves_to: EVENT_ID`.

Absence: not found — the message never entered durable ingress. Either it
never reached this relay (check the source transport itself) or ingress was
down at that moment; no delivery pipeline ever ran for it.

If you do not have the native id, list recent stored events (content-free
columns only):

```bash
sqlite3 /path/to/medre.sqlite "
  SELECT e.event_id, e.source_adapter, e.event_kind, e.created_at
  FROM canonical_events e
  LEFT JOIN delivery_receipts r ON e.event_id = r.event_id
  WHERE r.event_id IS NULL
  ORDER BY e.created_at DESC
  LIMIT 20;
"
```

Read: events stored but never attempted — recovery candidates. See
[recovery and replay](recovery-and-replay.md) for the full orphan queries,
including which receipt-less events are expected (no route matched, loop
suppressed) rather than lost.

Next: with the event id, continue.

## 5. Did the adapter accept the delivery?

```bash
medre inspect event EVENT_ID --recovery --storage-path /path/to/medre.sqlite
medre inspect receipts --event EVENT_ID --storage-path /path/to/medre.sqlite
```

Read, in order:

- The recovery runbook's `Current delivery guidance` — one line per
  destination with a disposition (`completed`, `in_progress`,
  `inspect_native`, `preview_replay`, `needs_operator_action`, ...). It
  names the exact destination, plan id, and route.
- Each receipt's `status`, `confirmation_level`, `attempt_number`, and
  `parent_receipt_id`. `sent` is adapter acceptance — the transport
  reported hand-off at the strength named by `confirmation_level`
  (`local_queue`, `local_transport`, `remote_service`, `end_to_end`).
  `failed` and `dead_lettered` are dispatch failures; follow the
  `parent_receipt_id` chain for retries.

Absence: an empty receipt array `[]` — the adapter never accepted a
delivery for this event (the event is an orphan candidate from step 4), or
every attempt failed. A `completed` disposition in the runbook is a local
fact; its own output line says it does not establish recipient delivery.

Next:

- Receipts `failed` or `dead_lettered` on the destination — go to step 8
  and the replay action below.
- A `queued` receipt — confirmation pending; go to step 6.
- Radio transports with a packet that never got a clear hand-off — go to
  step 7.
- `sent` receipts but the recipient still reports nothing — go to step 6
  for queue-backed transports, and accept that for fire-and-forget
  transports (radio) and server-level transports (Matrix), `sent` is where
  MEDRE's evidence ends. The remaining distance is real and unbridgeable
  from the relay alone.

## 6. Is it confirmation pending?

Queue-backed transports hand the message to an adapter-local queue; the
`queued` receipt records only that local acceptance. The outbox row rests
in the durable confirmation-pending disposition — never reclaimed, never
re-dispatched, never counted as delivered or failed:

```bash
medre inspect work-status --storage-path /path/to/medre.sqlite
```

Read: the `native queue` count per destination (greater than zero means
confirmation-pending work exists), plus the uncertainty totals line. The
view states its own boundaries in the output: connectivity unknown, owner
liveness unknown, recipient delivery unknown.

For one exact destination, preview the pending row (read-only):

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --resolution delivered
```

Read: `apply_authorized: false` with the `observed` block (`outbox_id`,
`expected_generation`, `expected_status: "queued"`,
`expected_updated_at`). This preview is the input to the resolution
action at the end of this page — nothing is decided by previewing.

Absence: `native queue 0` and no `queued` receipt — the delivery is not
confirmation pending. Resolution, when you choose to rule, is at
[confirmation-pending recovery](confirmation-pending-recovery.md); the
action section below states its irreversibility.

## 7. Is the handoff uncertain?

Radio transports (Meshtastic, MeshCore) can accept part of a multipart
packet without a definitive hand-off result. The evidence is per-part:

```bash
medre inspect work-status --storage-path /path/to/medre.sqlite
medre inspect radio-attempt OUTBOX_ID ATTEMPT --storage-path /path/to/medre.sqlite
```

Read: `uncertain outcome N` or reserved generations without attempt
evidence in `work-status`; per-part progress in `radio-attempt` — a part
with `state: "unknown"` and `handoff_recorded: false` is the uncertain
packet; `accepted` parts carry a recorded local hand-off reference. Both
outputs withhold content (`content_withheld: true`).

Absence: uncertainty totals all zero and every part `accepted` or
`unsent` — nothing is held uncertain for this delivery.

The recovery runbook names this fork `inspect_native`: possible native
acceptance, resolve the uncertainty first. An audited close of that review
is `medre resolve --action resolve_uncertain` — a recorded decision, not a
send (see [exact-target decisions](exact-target-decisions.md)); the action
section below covers its scope. An uncertain packet is never auto-resent
and never auto-abandoned: absence of evidence is not a negative answer.

## 8. Did content expiry change what can be read?

If the bridge enables `content_ceiling`, an event whose ceiling has
elapsed denies content reads and scrubs transport metadata from new native
references — replay can no longer re-render its content. The ceiling's
own read-only commands report which state an event is in:

```bash
medre storage ceiling audit --storage-path /path/to/medre.sqlite --event EVENT_ID
```

Read: `state` — `not_elapsed` (unchanged), `elapsed_pending` (expired;
content denied; not yet purged — the enforcement sweep's next candidate),
`residual_enforced` (enforced; late-write residuals remain fenced), or
`enforced_clean` (purged, receipt recorded). The report is content-free.

Absence: `not_elapsed` — expiry plays no role in this incident.

Purge happens only when `retry.ceiling_sweep` is enabled and the runtime
runs; by default it does not run, so an expired event stays
`elapsed_pending`. An enforcement receipt is not a physical-erasure
certificate; `physical_erasure_claimed` stays false. For the sweep
contract and the verification battery see
[the ceiling audit](content-ceiling-audit.md); a scoped post-sweep check
is part of the action section below. Note the ordering with replay: once a
ceiling has elapsed, replay of that event cannot restore or re-render the
expired content.

## Action commands, one target at a time

Every action below is scoped to one delivery identity — event, delivery
plan, adapter, channel — taken from the read-only exact preview. Nothing
here acts on a whole route, adapter, or sibling destination. Each step
names its precondition and what cannot be undone.

### Preview the exact target (read-only)

```bash
medre recover --event EVENT_ID --storage-path /path/to/medre.sqlite \
  --exact-plan-id PLAN --exact-adapter ADAPTER \
  --exact-channel 'CHANNEL'
```

Read: the observation fingerprint and `excluded_sibling_count` — the count
of destinations this scope deliberately excludes. Use `--exact-no-channel`
when the stored target has no channel. An absent or ambiguous identity
exits with code `5` and authorizes nothing. This is not a send command.

### Replay one delivery

Precondition: the underlying cause from steps 1–7 is fixed, the dry run
matches the intended scope, and you accept that replay sends a real
message that may duplicate one the transport already delivered. Preview
without side effects, then execute:

```bash
medre replay --mode dry_run --event EVENT_ID --config bridge.yaml
medre replay --mode best_effort --event EVENT_ID --config bridge.yaml --json \
  --exact-target-json '["EVENT_ID","PLAN","ADAPTER","CHANNEL"]'
```

Read the dry run's `by_route` first; a mistyped exact target matches
nothing and skips silently — treat an unexpected `skipped` count as a
wrong scope, not a success. After `best_effort`, the new receipt carries
`source: "replay"` and chains to the previous attempt through
`parent_receipt_id`; the earlier failure stays as immutable history and
the recovery scan stops listing the delivery once the current receipt is
`sent`. Replay cannot be unsent, and after a ceiling has elapsed it
cannot re-render the expired content (step 8).

### Rule one confirmation-pending delivery

Precondition: you have exclusive administrative authority over a private
(`0600`) state file, you have stopped conflicting tooling, and you have
reviewed the delivery's evidence. Apply only from a fresh preview,
copying all observed values exactly:

```bash
medre resolve --storage-path /private/state.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --resolution delivered \
  --apply --yes --decision-id UNIQUE_DECISION --actor-label LOCAL_OPERATOR \
  --outbox-id OBSERVED_OUTBOX --expected-generation OBSERVED_GENERATION \
  --expected-status OBSERVED_STATUS --expected-updated-at OBSERVED_VERSION
```

`--resolution` is `delivered`, `failed`, or `cancelled`. The ruling is a
terminal operator decision: it dispatches no send, fabricates no receipt,
and the ruled row is immutable — a refused apply records nothing (exit
`1` with the frozen refusal string). Repeating the same apply after a
committed ruling is refused. Full contract:
[confirmation-pending recovery](confirmation-pending-recovery.md).

### Cancel or decide one unstarted delivery

Cancellation of one unstarted destination, and the audited
`resume_scheduled` / `resolve_uncertain` decisions, are separate commands
with their own previews and refusals. They never send and never widen to
siblings; follow
[exact cancellation](exact-cancellation.md) and
[exact-target decisions](exact-target-decisions.md). Read back the durable
audit record with `medre inspect decisions --event EVENT --storage-path
/private/state.sqlite` (see [operator decisions](operator-decisions.md)).

### Verify ceiling enforcement (read-only)

If step 8 found `elapsed_pending` and the enabled sweep has since
processed the event, audit the event again and verify its receipt:

```bash
medre storage ceiling audit --storage-path /path/to/medre.sqlite --event EVENT_ID
medre storage ceiling verify --storage-path /path/to/medre.sqlite --event EVENT_ID
```

Read: audit `state: "enforced_clean"` with the receipt counters, and
verify `verdict: "passed"`. A missing receipt is a failed, fail-closed
verification (`event_state: "no_receipt"`): absence of proof is not
proof. Both commands are read-only and never purge.

## A deliberately failed recovery, and what it should look like

On a disposable copy of your state file, apply a resolution with a
deliberately stale `expected-generation`:

```bash
medre resolve --storage-path /private/copy.sqlite --event EVENT \
  --plan-id PLAN --target-adapter matrix --target-channel '!room:server' \
  --resolution delivered \
  --apply --yes --decision-id FAILED_DECISION --actor-label LOCAL_OPERATOR \
  --outbox-id OBSERVED_OUTBOX --expected-generation WRONG_GENERATION \
  --expected-status OBSERVED_STATUS --expected-updated-at OBSERVED_VERSION
```

Expected: exit code `1`, `Resolution refused: Stale observed state;
nothing decided` on stderr, and no change to the database — the guard
refuses the whole write instead of applying a stale ruling. Re-run the
preview and copy its values exactly to recover from the refusal. The same
fail-closed behavior guards every action above: a competing claim, a newer
generation, a moved update version, or a reused decision id is refused
with nothing recorded.

## Known limitations

- No MEDRE command observes a recipient. Steps 5–8 end at local facts by
  design; the final check is always the destination client or device.
- `work-status` and `recover` read one database snapshot each invocation;
  pages are live views, not snapshots. Re-run against the same file after
  any action.
- Diagnostics with `--refresh-health` starts the real runtime briefly; it
  observes poll-time state once, not the window of the incident.
- Fire-and-forget transports cannot answer step 6 or 7 beyond the recorded
  part states; transports differ in what `confirmation_level` they can
  report, and none reports a recipient.
- Replay duplicates by design across different or empty run ids; a named
  run claims one durable target generation per identity. See
  [recovery and replay](recovery-and-replay.md) for the duplicate-risk
  table.
- The ceiling audit covers the logical SQLite census only; transport
  queues, backups, exports, logs, and SQLite free pages remain documented
  unmeasured scopes.
