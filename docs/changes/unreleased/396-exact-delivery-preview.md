# Exact delivery recovery preview (read-only)

Add a fail-closed event/plan/adapter/channel selector to `medre recover`.
Display an observation fingerprint, current attempt authority and excluded
sibling count without broad replay commands or any delivery side effects.
Write-capable exact recovery remains intentionally unavailable.

Publish the exact-preview JSON Schema, omit nested action commands, and
reject incomplete selectors before opening storage.

Normalize all exact identity comparisons consistently, distinguish not-found
selection failures from invalid arguments, and preserve unrelated data errors.
Cover no-channel and receipt-only persisted evidence and stable fingerprints.
