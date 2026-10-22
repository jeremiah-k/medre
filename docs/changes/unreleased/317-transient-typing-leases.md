# Bounded transient typing leases

Add immutable typing activity and destination contracts plus a bounded in-memory
lease pool. Aggregate multiple actors into relay activity, expire stale claims,
coalesce refreshes, preserve existing leases on capacity refusal, and clear
emitted indicators when resetting state.

Keep composing activity separate from canonical history, durable delivery,
verified actor identity, and puppeting. Define Matrix replacement-list and
XEP-0085 conversion obligations. Routes explicitly opt in with `forward_typing`
on concrete channel mappings.
Runtime source binding, static allowlists, bounded native calls, permission
withdrawal, and cancellation-safe shutdown remain separate from message delivery.
Providers require a declared typing capability and a native sender port.
