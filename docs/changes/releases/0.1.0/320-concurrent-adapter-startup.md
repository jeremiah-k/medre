# Start independent adapters concurrently

Own adapter startup tasks in one structured group after core preparation.
Slow connections and failed-start cleanup do not delay independent starts.
Keep ingress, retry and typing activation behind the completion barrier, retain
stable summaries and reverse identifier shutdown order, and roll back partial
and ready adapters together when startup is cancelled.

Reject `stop()` while startup still owns core resources and pending adapters,
preventing a premature storage close and invalid terminal-state transitions.
Callers cancel and await the startup task for coordinated rollback.

Preserve cancellation requests drained during orchestration-error rollback,
so an external shutdown cannot be silently replaced by a startup exception.

Retire partial core resources when preparation is cancelled or fails. Keep CLI
startup cancellation attached to the startup owner's rollback, including repeated
cancellation, without a second independent teardown.

Separate task-group coordination from caller cancellation accounting. Report a
self-cancelled provider as a startup error, preserve grouped infrastructure
failures, and give simultaneous caller cancellation priority after owned rollback.
