# Content availability policy preview

Introduce a pure, versioned preview model for retained/unavailable/denied
content and scope-limited consumer holds. The new functions are not installed
in the runtime admission or deletion path; persisted content identity, content
expiry and plugin permissions remain separate safety-gated changes.

Require an optional requester's live hold before revealing content presence;
keep mandatory dependency holds internal, respect issue times, and validate
metadata against a versioned decision schema.

Require an explicit requester context and validate constructed decisions before
serializing timezone-aware UTC metadata. Cover ceiling clamping and expiry for
optional requesters without relying on optional date-time checker packages.
