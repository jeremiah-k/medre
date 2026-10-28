# Adopt the LXMF restore observation revision

Pin the LXMF adapter to the merged revision whose restore qualification
requires a fresh post-restore announce before delivery is attempted: the
restored endpoint advertises a unique display name while keeping its LXMF
address, so queued pre-shutdown announces cannot satisfy restored readiness.
Inbound deadlines are unchanged, and an exceeded deadline now reports bounded
peer message, link, and cached worker diagnostics instead of a bare timeout.
Adapter runtime code is unchanged; the revision is confined to qualification
helpers, tests, and its own change fragment.
