# Durable Canonical Plugin Execution

Add explicitly assembled enrichment, transformation, and policy execution to
inline and durable pipeline processing. Freeze plan identity before invocation,
including empty plans. Serialize instances, bound attempts and expansion, and
reuse committed outcomes after interrupted acknowledgements. Derived events
continue after their producing instance through all downstream gates.

Buffer instance state within active invocations and commit it only with successful
decisions. Quarantine contract/capability violations, apply the ingress poison-work
budget to settled required failures and exhausted retries, and reject late state access after deadline cancellation. Report incomplete cleanup.

Keep contracts in the kernel with public author re-exports. Reserve context
services without implementations. Require explicit routes for plugin.custom
output so observational enrichment is stored without catch-all broadcasting.
Runtime YAML and CLI activation remain separate work.

Plugin-configured pipelines reject routing and delivery replay until replay can
preserve the qualified frontier. Historical inspection and re-rendering remain
available.

Keep operational retry delays, pending cancellation, shutdown, and stale
uncommitted state retryable without spending the ingress failure budget.
Plan conflicts and corrupt evidence consume the bounded processing budget.

Validate every configured instance before loading implementations. Factories
reserve resource acquisition for host-owned lifecycle initialization.
