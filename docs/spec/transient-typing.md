# Transient Typing Activity

## 1. Scope and authority

`TypingActivity`, `TypingSnapshot`, `TypingTarget`, and `TypingUpdate` define
transient composing activity. `TypingLeasePool` implements bounded in-memory lease aggregation.
RuntimeBuilder owns an optional TypingRelay around this pool. Routes opt in
using `forward_typing: true`; default message routes do not forward activity.
Both endpoints must provide a TypingSender port and declare `typing: true`.
Replay startup does not activate the relay.

Typing activity MUST NOT be admitted as a canonical event, stored in the event
log, placed in the durable outbox, replayed, or represented by delivery receipts.
Existing `presence.changed` facts retain their independent canonical semantics.
This contract does not add an event kind, modify an event schema, confer device
operation privileges, or create a second message-delivery pipeline.

Runtime integration MUST bind source adapter identity at the adapter context and
MUST authorize concrete destinations under explicit operator configuration
before calling the pool. Activity MUST NOT expand its own routes or bypass
sender, channel, or destination restrictions. The pool takes authorized targets;
it is not a route-policy evaluator. Forwarding MUST be opt-in, including in
rooms with encrypted messages, because activity disclosure is distinct from
message content delivery.

## 2. Values

`TypingActivity` carries an opaque source channel, native sender identifier,
boolean composing state, and finite duration greater than zero and at most
30 seconds. Source identifiers are nonempty strings of at most
4,096 Unicode code points, excluding controls, line separators, and unpaired
surrogates. Valid Unicode joiners and exact native spelling are preserved.
The native sender is a transport claim, not proof
of a reconciled or verified actor. Runtime binds the separate source adapter ID.

`TypingSnapshot` replaces the complete native actor set for one source
conversation. It carries a unique tuple of at most 1,024 native senders and the
same freshness bound. An empty tuple releases all actors in that conversation.
Omitted actors MUST be released even when capacity refuses other actors in the
snapshot. Snapshot processing can admit a subset at capacity; a false admission
result indicates incomplete admission, not rollback of releases. Providers MUST
NOT maintain a second actor cache to compute replacement-list differences.

`TypingTarget` is one concrete adapter and conversation, with the same bounded
opaque identifier rules. It carries no source actor or puppet identity.

`TypingUpdate` is aggregate desired activity at a destination. A positive timeout
requests a finite indicator lifetime; zero requests a clear. Generated updates
MUST NOT be described as acknowledged native delivery. Targets use the relay's
own native identity; the contract MUST NOT impersonate the original actors.

The values are immutable Python contracts, not serialized canonical envelopes.
Native codecs own address validation and conversion of seconds to protocol units.

## 3. Leases and bounds

A lease is keyed by source adapter, source channel, and native sender. A stop
releases only that source's lease. Each target remains active while at least one
unexpired source contributes to it, using the latest contributing deadline.
The pool MUST use a finite nondecreasing monotonic clock, independently of
wall-clock corrections and adapter-provided timestamps.

`observe` replaces one source's authorized target set atomically. Capacity
refusal MUST preserve the previous unexpired lease and MUST NOT evict another
actor. Empty authorized targets withdraw the source's contribution. A stop MUST
remain admissible at capacity. Exact source adapter/channel echoes are ignored;
repeated destination grants coalesce.

Default bounds are 1,024 active sources and 64 active destinations. Each source
references at most the destination bound. At most another 64 emitted destinations
can await a clear. Rapid remapping MUST NOT accumulate a historical output queue.
Stopped, expired, and reset leases carry no replay authority.

`poll` expires leases and emits clears, initial positives, shortened aggregate
lifetimes, and due refreshes. Output is deterministic by adapter/channel, with
clears preceding positives. Refresh defaults to five seconds and uses half of a
shorter emitted lifetime, with a half-second scheduling floor. Expiry is the
final deadline. Refresh scheduling MUST advance toward expiry rather than
creating an infinite sequence of half-life timers.

The caller MUST poll immediately after accepted changes and by `next_deadline`
while active. The pool starts no task and creates no timer per source. `reset`
forgets all actors and returns best-effort clears for previously emitted targets.
It neither retries a failed send nor promises native cleanup on process death.
Adapters MUST document native timeout limitations.

## 4. Native protocol conversion

[Matrix typing notifications](https://spec.matrix.org/latest/client-server-api/#typing-notifications)
are room-scoped ephemeral state. Each `m.typing` user list replaces the previous
list; it is not a list of incremental starts. A Matrix ingress implementation
MUST release previously observed actors omitted from the replacement list and
ignore its own relay user. A Matrix sender renews its own finite indicator and
clears it when aggregate activity ends.

[XEP-0085](https://xmpp.org/extensions/xep-0085.html) defines five chat states,
including composing, and supports groupchat as well as direct conversations.
Typing projection is a subset of these states; it MUST NOT be presented as full
presence or full chat-state fidelity. Native adapters MUST respect negotiation,
message type, and groupchat rules. In particular, standalone repeated composing
notifications are forbidden: internal refresh requests MUST NOT become repeated
identical XMPP notifications. Groupchat `gone` handling follows XEP-0085 rather
than being interpreted as a generic offline event.

The 30-second lease is a local freshness limit. XMPP does not give a composing
notification the same server-managed timeout as Matrix. A lost stop can therefore
cause a temporary projection difference; neither bounded observation nor a
successful local clear establishes exactly-once notification delivery.

## 5. Runtime integration

`forward_typing` MUST be an explicit boolean and requires concrete source and
destination channels, including every expanded context mapping. Structured
message destinations and wildcard activity grants are rejected. The flag is
independent of canonical event-kind filters; every static source, destination,
sender, room, and channel allowlist remains authoritative through the existing
route-policy evaluator. Multiple granting routes coalesce their destinations.

The runtime MUST bind `AdapterContext.publish_typing` to the configured source
adapter. Publication returns local admission only, never durable acceptance or
native delivery. A positive update with no authorized destination is refused;
stops release existing source-scoped leases even after grant withdrawal.

TypingRelay accepts activity after adapter startup and only during live scope.
It owns one worker, at most eight concurrent native calls, and at most two
seconds per call. Before sending, it rechecks permissions and recomputes the
latest desired state; queued work cannot revive an expired or stopped actor.
Permission withdrawal is reconciled at least once per second between bounded
send batches, without renewing any lease. Native send failures are counted and
logged without exception bodies; the message pipeline remains independent.

Shutdown MUST refuse publication, reap owned calls, and attempt clears while
adapter sessions remain open. Caller cancellation MUST NOT abandon this owned
cleanup. Runtime reactivation starts with empty activity state. Native protocol
limits and best-effort clearing still apply; there is no persisted replay,
receipt, or retry queue for activity.
