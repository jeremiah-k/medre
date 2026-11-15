# Admit original-attempt radio packet observations

Persist packet-scoped native observations for an already accepted original
handoff after a newer retry generation is reserved. The SQLite writer checks
immutable packet evidence and parent delivery identity within the insertion
transaction. Invalid native handles, wrong ordinals, unsupported confirmation
levels and unrelated delivery claims are rejected. Generic observations keep
current-generation fencing; this new interface neither sends a packet nor
creates a receipt or changes the parent lifecycle.

Attempt correlation rejects booleans and floating-point values even when Python
numeric equality would match an integer generation.
