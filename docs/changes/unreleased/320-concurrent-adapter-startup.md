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
