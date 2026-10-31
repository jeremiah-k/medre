# Radio multipart text

Merged core #343 and MeshCore #13 add opt-in multipart ordinary text.
Short messages retain their existing wire form. The default `max_text_parts: 1`
retains bounded single-packet rendering. Set a larger limit only with the reviewed
core and adapter revisions installed together:

```yaml
adapters:
  meshcore:
    meshcore:
      max_text_parts: 16
      message_delay_seconds: 0.5
```

Keep the rest of the adapter's existing connection and route configuration.
Each packet repeats attribution and a numbered marker within the effective UTF-8
budget, including firmware sender-name overhead. Exceeding the part limit rejects
the whole plan before sending; it does not silently truncate it. Pacing is shared
with other sends and fanout through the existing session owner.

## Meshtastic

Merged Meshtastic #15 uses the same `max_text_parts` opt-in. With that
provider installed against merged core #343 or newer, add the limit to each selected
Meshtastic instance while retaining its existing connection and route settings:

```yaml
adapters:
  meshtastic:
    FXLF:
      max_text_parts: 16
      message_delay_seconds: 2.2
```

Core keeps the multipart delivery in progress while each packet waits for its
native handoff through the paced queue. Ordinary messages retain deferred queue
admission. A stopped or replaced client, cancellation, missing native packet ID
or uncertain SDK call holds the remaining message for inspection. No individual
packet acknowledgement means the whole message reached a recipient.

The queue measures its gap from completion so slower grant/SDK work cannot cause
the following message to burst. Cancelling a native operation already executing
in a thread cannot prove that it stopped; retain its uncertain evidence. Partial
messages with a proven-unsent tail can continue through the configured retry
worker after a reconnect or restart. Accepted packets are retained; unknown
submissions remain held. Existing retry limits and backoff apply. Select only
the intended reviewed sources and keep hardware evidence
separate from local SDK handoffs. Defaults and package versions are unchanged.

## Troubleshoot an interrupted multipart message

A local SDK acceptance does not prove that another radio received the packet.
If a call times out or a process stops during submission, the attempt stays
uncertain and automatic resend stops. Previously accepted packets are retained.
Inspect the exact outbox ID and attempt generation from the delivery evidence:

```bash
medre inspect radio-attempt outbox-EXAMPLE 1 --storage-path /path/to/medre.sqlite
```

For a read-only assessment of whether the retained state is a candidate for
separately authorized continuation, request:

```bash
medre inspect radio-attempt outbox-EXAMPLE 1 --storage-path /path/to/medre.sqlite --assess-continuation
```

`review_candidate` means there is an accepted prefix, an unsent tail, and
parseable, consistent native payload fingerprints. An explicit native channel
that disagrees with the retained target keeps the attempt held; an absent native
channel does not manufacture a contradiction. `held` identifies a missing,
contradictory or uncertain fact. Neither status grants a native call. A separate runtime
operation must verify the original accepting attempts, exact latest history,
matching channel rendering, and a new independently owned outbox claim before
sending through the existing paced adapter. The CLI opens storage read-only,
including when retained evidence is malformed; it does not claim work, recover
submitting packets, or send anything.

The JSON contains the plan fingerprint, per-packet state and a
`handoff_recorded` presence flag, alongside current parent identity/status.
The planned wire text and scoped handoff references are withheld. Inspection
does not start adapters, mutate progress, recover submissions or authorize another
send. A submitting packet remains submitting in the report; after explicit
owned recovery it becomes unknown. Neither state is proof of RF delivery.
The configured retry worker can continue a proven-unsent suffix under a fresh
owned attempt. Changed rendering or channel semantics keeps the message held.
Retain uncertain evidence for investigation; inspection does not force resend
or promote unknown packets.

Deterministic tests cover rendering, session send ownership, storage and core
aggregation. The [native evidence index](../dev/native-evidence-index.json)
records bounded physical Meshtastic and MeshCore process-crash qualification:
an accepted prefix continued without duplicate packets, and an interrupted
uncommitted handoff caused no additional native submissions. The run used
normal lease expiry and independent receivers, with eight-second late-duplicate
windows. Radio firmware resets, host power loss and longer outages remain outside
that evidence. Native packet observations keep their protocol-specific meaning;
these external receiver logs do not change delivery confirmation levels.

The required radio storage table was introduced in core #342; old prerelease
storage adoption follows [the explicit backup/reset procedure](../spec/storage.md#105-pre-release-stale-database-reset).
This handoff change introduces no additional database shape change.

For the internal continuation contract, see
[radio text planning](../spec/radio-text-planning.md#explicit-continuation-of-a-proven-unsent-tail).

## Content-safe inspection

`medre inspect radio-attempt` reports parent identity, plan fingerprint and
per-packet state with a `handoff_recorded` presence flag. It withholds the retained wire-text plan and marks
`content_withheld: true`. Continuation assessment uses native handoff facts;
it grants no send and does not validate withheld wire content. Full plan
validation remains part of the owned runtime continuation transaction.
