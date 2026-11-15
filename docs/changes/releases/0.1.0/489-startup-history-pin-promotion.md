# Promote startup-history and pre-start buffering pins

Promote the merged Matrix provider revision that suppresses startup history
before decode on every ingress path. Each active adapter lifetime captures a
wall-clock baseline before its first sync; events whose origin server
timestamp predates it by more than `startup_backlog_suppress_seconds`
(default 60 seconds; `0` disables) never forward, whichever path delivered
them. The tolerance keeps federation lag and minor clock skew flowing, and
suppression counts under `inbound_suppressed_stale` without blocking durable
acknowledgement.

Promote the merged MeshCore provider revision that buffers inbound payloads
delivered while `start()` is in flight — the pinned SDK drains a small device
buffer inside session startup — and flushes them through normal admission in
arrival order once started. The buffer is bounded at 64 frames, snapshots
caller-owned mappings, counts overflow drops with content-free warnings, and
is discarded on stop, failed, or cancelled start; callbacks installed by a
stopped or superseded session never enter a replacement lifecycle.

Publish `startup_backlog_suppress_seconds` in the authoritative Matrix
adapter-config schema arm with this pin, keeping schema and source in lockstep.
