# Clarify startup and handoff logs

Startup distinguishes configured routes from expanded routes and moves
individual route registrations, adapter construction, and duplicate startup
details to DEBUG. A total adapter startup failure is labeled as failed rather
than ready. Delivery logs distinguish deferred queue acceptance from a
synchronous handoff, report the adapter confirmation level, and retain full
plan identifiers in structured JSON extras.
