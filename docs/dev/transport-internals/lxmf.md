# LXMF transport internals

Native worker and SDK behavior behind the
[LXMF transport setup guide](../../ops/transport-setup/lxmf.md): what MEDRE
sees from Reticulum objects, delivery threading, and router shutdown
ownership. The normative contract lives in the
[LXMF transport profile](../../spec/transport-profiles/lxmf.md).

## What MEDRE Sees

MEDRE never handles `RNS.Identity` or `RNS.Destination` objects directly. The adapter converts them to strings:

- `source_transport_id`: the sender's LXMF delivery-destination hash as a
  32-char hex string — not their identity hash. The codec uses the same
  hash as the peer's delivery-destination channel, which is how inbound
  events correlate with route destination addresses.
- `native_message_id`: the LXMF message hash as a 64-char hex string.
- Core and pipeline code never import from `lxmf` or `RNS`.

## Async Delivery Caveats

### Threading Model

Reticulum and LXMF use native background threads. MEDRE runs the real
`LxmfSession` inside an owned subprocess; the worker normalizes SDK callbacks
and forwards bounded protocol frames to the parent. MEDRE core does not
construct or own native RNS SDK objects. The fake mode remains SDK-free.

### Outbound Async Behavior

`LxmfSession.send_text()` creates an `LXMessage`, registers delivery state callbacks, and calls `router.handle_outbound(lxm)`. It returns `(native_message_id, initial_state)` immediately — typically `OUTBOUND` or `SENDING`, not `DELIVERED`. Actual delivery happens asynchronously.

MEDRE persists callback-emitted terminal LXMF states as **post-handoff delivery
observations** tied to the exact durable outbox attempt. The pinned SDK emits
successful delivery through its delivery callback and some failures through a
separate failed callback; MEDRE registers both and does not invent terminal
states the SDK did not report. These observations appear in `medre trace`,
`medre inspect`, and evidence bundles. They do not rewrite the original
`sent/local_queue` receipt or reopen terminal outbox state.

Treat the persisted state as the LXMF provider's report, not as a stronger
MEDRE proof claim. In particular, an LXMF `delivered` observation currently
keeps `confirmation_level="unknown"`; MEDRE does not promote it to
`end_to_end` automatically.

The SDK-message correlation needed to create a post-handoff observation is
process-local until the observation is persisted. A hard crash after LXMRouter
handoff but before the SDK reports a later delivery state may therefore leave
only the original `sent/local_queue` receipt. MEDRE does not fabricate a terminal state during restart recovery.

Outbound retry is bounded: at most 3 total attempts per send with short
linear backoff. After exhaustion, the send raises `LxmfSendError`, which
the adapter normalizes to a transient `AdapterSendError`. An exception
raised by `router.handle_outbound()` is not retried: the router may have
already accepted the message, so the session raises
`LxmfHandoffUnknownError` instead — an uncertain native handoff that
recovery must reconcile rather than resubmit.

### Reticulum Singleton

`RNS.Reticulum()` is a singleton **inside each native worker process**. It
can attach to an existing shared Reticulum service; a second constructor in
the same process is not required. This means:

- The worker reuses a Reticulum instance already constructed in its own process.
- Each real MEDRE LXMF profile has its own native worker, identity, router,
  and handoff journal. Separate profiles can attach to the same network service
  without claiming ownership of the service's state. Do not share a worker
  profile directory with another application.
- Test isolation requires custom config directories.

## Router Shutdown Ownership

The native worker owns each `LXMRouter` it creates. It restores signal
handlers installed by native constructors and keeps its profile leases held
through native exit persistence. The parent also retains process ownership
until the worker exits, including after its Python-level cleanup. MEDRE does
**not** shut down an external/shared Reticulum service when stopping a profile.

The pinned LXMF SDK does not expose a join/stop primitive for all of the
router's background jobs. Process isolation bounds their lifetime: the owner
must reap the worker rather than interpret a returned session-stop call as
proof that every native thread has exited.
