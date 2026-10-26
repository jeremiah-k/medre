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

A local SDK acceptance does not prove that another radio received the packet.
If a call times out or a process stops during submission, the attempt stays
uncertain and automatic resend stops. Previously accepted packets are retained.
Inspect the exact outbox ID and attempt generation from the delivery evidence:

```bash
medre inspect radio-attempt outbox-EXAMPLE 1 --storage-path /path/to/medre.sqlite
```

The JSON contains the exact planned wire text, per-packet state and scoped local
handoff references, alongside current parent identity/status. Inspection does
not start adapters, mutate progress, recover submissions or authorize another
send. A submitting packet remains submitting in the report; after explicit
owned recovery it becomes unknown. Neither state is proof of RF delivery.
Automatic restart does not resume a partially accepted multipart message in this
implementation. Retain this evidence and hold uncertain work for investigation. There is no
operator command here to force resend or promote unknown packets.

Deterministic tests cover the renderer, real session send owner, storage, core
aggregation and failures without hardware. Installed-SDK contracts and a bounded
physical MeshCore qualification are still required before describing this path
as hardware-qualified. The coordinated Meshtastic candidate extends this path through its existing
paced queue; see the following section before selecting that source.

The required radio storage table was introduced in core #342; old prerelease
storage adoption follows [the explicit backup/reset procedure](../spec/storage.md#104-pre-release-stale-database-reset).
This handoff change introduces no additional database shape change.

## Meshtastic candidate

The coordinated Meshtastic PR uses the same `max_text_parts` opt-in. With that
candidate installed against merged core #343, add the limit to each selected
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
messages do not automatically resume after restart. Select only the intended
candidate sources, qualify them independently, and keep hardware evidence
separate from local SDK handoffs. Defaults and package versions are unchanged.
