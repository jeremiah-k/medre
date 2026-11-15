# Read-only content dependency preview

Add `medre inspect content --event EVENT --storage-path PATH` to display
possible core delivery dependencies and content-bearing row counts without
reading content bytes. No retention policy, schema migration, deletion or
provider-owned queue manipulation is introduced.

Count incoming reply relations and delivery observations in the same SQLite
snapshot; conservatively flag processing, failed and unknown work states.
Explicitly identify unmeasured canonical content and physical SQLite remnants.
