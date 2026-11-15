# Preserve native health in all provider diagnostics

Qualify XMPP, Chatmail, and SimpleX providers that publish known health and
mode through common diagnostic fields while retaining native state aliases.
Unobserved connectivity, unavailable reconnect/error details and uncounted
statistics remain explicit unknowns rather than fabricated success or zeros.
SDK-free fakes identify their mode without claiming native connectivity.

Check normalized health against explicit health probes for all seven providers
and align the diagnostic specification with nullable observations.

Populate absent common fields from same-named session observations, retaining
explicit adapter-level unknowns, false values and zero counters. Preserve safe
native session detail and sanitize promoted values. Value-level regressions
cover mapping, dataclass, msgspec and public-attribute snapshots.
