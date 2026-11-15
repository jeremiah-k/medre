# Preserve completed fanout work across a fresh runtime generation

Add a regression using durable SQLite ingress, outbox and retry processing with
four fake targets. One target fails before native handoff while three complete.
Reopening storage suppresses duplicate ingress and retries only the unfinished
target, preserving completed rows and receipt history. Scheduling uses the
retained retry deadline directly, without sleeps or editing the outbox.
