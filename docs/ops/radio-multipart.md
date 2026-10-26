# MeshCore multipart text

The coordinated core and MeshCore changes add opt-in multipart ordinary text.
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
medre inspect radio-attempt obox-EXAMPLE 1 --storage-path /path/to/medre.sqlite
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
as hardware-qualified. Meshtastic multipart delivery is not enabled by this work.

The required radio storage table was introduced in core #342; old prerelease
storage adoption follows [the explicit backup/reset procedure](../spec/storage.md#104-pre-release-stale-database-reset).
This handoff change introduces no additional database shape change.
