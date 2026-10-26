# Execute synchronous radio plans through guarded native handoffs

Core now binds a complete renderer-owned radio plan and dispatches each packet
through an explicitly participating synchronous adapter. A one-use grant commits
inside the adapter's paced send owner immediately before its native call. Each
local handoff retains scoped evidence; uncertainty stops automatic resend and
only complete committed plans reach the parent delivery lifecycle.

Multipart aggregates do not select an arbitrary native packet as a whole-message
reply target. `medre inspect radio-attempt OUTBOX_ID ATTEMPT --storage-path PATH`
shows retained progress read-only, including ambiguous submitting records.

MeshCore's coordinated adapter PR adds explicit `max_text_parts` opt-in. Deferred
Meshtastic multipart sending remains pending its queue ownership boundary.
No package version, SQLite shape or schema marker changes are introduced here.
