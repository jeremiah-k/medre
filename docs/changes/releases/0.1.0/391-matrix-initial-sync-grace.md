# Wait for Matrix initial sync before classifying startup

Qualify the Matrix provider with a bounded first-sync startup wait. After
login and room joins, allow 45 seconds by default for acknowledged native
progress before returning startup health. Configure
`initial_sync_timeout_seconds` to change the budget, or zero to report pending
health immediately. Native errors and reconnects end the wait immediately.
Expiry retains degraded health and the existing recovery owner; later progress
updates current health automatically.

The runtime startup deadline remains authoritative for the entire attempt.
Cancellation closes partial resources, and durable startup admission remains
available during the wait. Schema and operator references describe both budgets.

Stop and replacement retire pending initial-sync waits without publishing stale
readiness or clearing the replacement's native session. Operator examples use
the runtime's actual command, environment and log surfaces.
