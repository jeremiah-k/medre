# Qualified compatibility merges

CI now rejects adapter compatibility pins that have not landed in the adapter's
default branch. `Merge readiness` aggregates required qualification tiers so a
failed or unexpectedly skipped dependency cannot produce a green merge gate.
The development guide describes test ownership, explicit candidate-pair testing,
and dependency-first merges without persistent core-ref overrides.
