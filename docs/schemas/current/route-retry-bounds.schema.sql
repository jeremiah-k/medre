-- Content-free retry bounds for one outbox generation, captured at admission
-- from the accepted route's resolved retry policy.  Written once atomically
-- with the outbox row and never rewritten or purged: numeric/boolean policy
-- facts carry no message content or content-derived bytes.  After ceiling
-- enforcement retires the frozen dispatch context, post-erasure reconciliation
-- reads these bounds instead of any current configuration.  Their presence
-- governs retry-budget arithmetic only; it never authorizes another native
-- send.
CREATE TABLE IF NOT EXISTS delivery_route_retry_bounds (
    outbox_id TEXT PRIMARY KEY REFERENCES delivery_outbox(outbox_id),
    max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
    backoff_base REAL NOT NULL CHECK (backoff_base > 0),
    max_delay_seconds REAL NOT NULL CHECK (max_delay_seconds > 0),
    jitter INTEGER NOT NULL CHECK (jitter IN (0, 1))
);
